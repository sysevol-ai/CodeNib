# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Per-instance experiment harness for the agent-compile cost study.

Given a :class:`~scripts.agent_compile.lib.config.SweepConfig`, this module
loads the dataset rows, builds the *full* (union-of-all-subsets) skill context
once per instance from prebuilt indexes, classifies the scenario, and runs a
single agent cell (``run_cell``) for a given skill subset.

These functions were private helpers inside ``run_agent_sweep.py``; they are
promoted here so the sweep runner *and* the offline retrieval ablations share
one definition of "load this instance + register skills" instead of importing
underscore-prefixed names from a sibling script.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .config import SweepConfig

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# language_group (HF column) -> classify() language key
LANG_GROUP_TO_KEY = {
    "Python": "python",
    "Go": "go",
    "Rust": "rust",
    "C++/C": "cpp",
    "TypeScript/JavaScript": "typescript",
}


def slug(value: str) -> str:
    """Filesystem-safe slug for cell ids."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")


# ---------------------------------------------------------------------------
# Dataset + scenario
# ---------------------------------------------------------------------------


def load_dataset_rows(cfg: SweepConfig):
    """Return ``(rows_by_id, eval_lookup)`` for the configured instances.

    An empty ``cfg.instances`` means the *whole* split — every instance is
    returned (the runner then skips any without prebuilt indexes).
    """
    from codeminer.dataset.codeminer_base import CodeMinerBaseDataset

    ds = CodeMinerBaseDataset(dataset=cfg.dataset, split=cfg.split)
    data = ds.load()
    if cfg.instances:
        wanted = set(cfg.instances)
        rows_by_id = {r["instance_id"]: r for r in data if r["instance_id"] in wanted}
    else:
        rows_by_id = {r["instance_id"]: r for r in data}
    eval_lookup = ds.load_eval_metadata()
    return rows_by_id, eval_lookup


def scenario_for(query: str, language_key: str) -> str:
    """Deterministic ``language:has_stacktrace`` scenario key for a query."""
    from codeminer.agent.compile import classify
    from codeminer.compiler.params import SessionContext

    sctx = SessionContext(repo_path=".", repo_size=1, primary_language=language_key)
    return classify(query, sctx).key()


# ---------------------------------------------------------------------------
# Per-instance index loading (full set, once) + per-cell agent run
# ---------------------------------------------------------------------------


# Pre-load recipes name retrievers ("embedding"/"bm25"), not skills; map them to
# the skill that backs each index so the contexts get built even when no ARM
# offers that retriever as a tool (pre-load arms only carry default tools).
_PRELOAD_RETRIEVER_SKILL = {
    "embedding": "embedding_search",
    "bm25": "bm25_search",
    # graph = the codeminer_context composer (search seeds + call-graph expand);
    # pulling it into the union builds the "expand" (symbol_graph) context.
    "graph": "codeminer_context",
}


def all_index_skill_ids(cfg: SweepConfig) -> List[str]:
    """Union of skills across all subsets — used to load the full context once.

    Also includes the skills implied by any ``preload`` recipe, so a pre-load
    arm whose subset is just default tools still gets its retrieval index built.
    """
    seen: List[str] = []
    for skills in cfg.subsets.values():
        for s in skills:
            if s not in seen:
                seen.append(s)
    for recipe in (cfg.preload or {}).values():
        for retriever in recipe.get("retrievers") or []:
            sk = _PRELOAD_RETRIEVER_SKILL.get(retriever)
            if sk and sk not in seen:
                seen.append(sk)
    return seen


def load_full_contexts(cfg: SweepConfig, repo_path: str, cache_dir: str):
    """Build the full (union) context dict + register every skill once."""
    from codeminer.agent.skills.loader import SkillLoader
    from codeminer.agent.skills.registry import SkillRegistry
    from codeminer.compiler import build_skill_contexts

    skills_dir = os.path.join(_PROJECT_ROOT, "codeminer", "agent", "skills")
    union = all_index_skill_ids(cfg)

    # 1) metadata-only load so build_skill_contexts can read index_requirements
    SkillRegistry().reset()
    SkillLoader().load_all(skills_dir, contexts=None)

    contexts = build_skill_contexts(
        repo_path=repo_path,
        skill_ids=union,
        languages=["python"],  # bm25 builds from the prebuilt graph; lang-agnostic
        cache_dir=cache_dir,
        embedding_model=cfg.embedding_model,
        embedding_dimension=cfg.embedding_dimension,
        default_top_k=cfg.topk,
        default_level="l2",
        rebuild=False,
    )

    # 2) re-load with contexts so executors are wired
    SkillRegistry().reset()
    SkillLoader().load_all(skills_dir, contexts=contexts)
    return contexts


def _leaf_symbol(name: Any) -> str:
    """Return the final answer/GT leaf from graph or display symbol names."""
    s = str(name or "").strip()
    if not s:
        return ""
    if ":" in s:
        s = s.rsplit(":", 1)[1]
    if "]" in s:
        s = s.rsplit("]", 1)[1]
    s = s.replace("#", ".")
    s = s.split("(", 1)[0]
    return s.split("/")[-1].split(".")[-1].strip()


def build_symbol_span_index(prebuilt_dir: str, instance_id: str) -> Dict[Any, Any]:
    """``{(norm_file, leaf_name): (start_1based, end_1based)}`` from the prebuilt
    graph, so committed scoring can resolve a named symbol to its line span.

    Graph vertices are 0-based (tree-sitter); shifted +1 here to match the
    1-based ground-truth blocks. Returns an empty dict when no graph is present.
    """
    from codeminer.eval.retrieval_eval import normalize_file_path
    from scripts.agent_compile.lib.prebuilt import load_prebuilt_code_graph

    path = os.path.join(prebuilt_dir, instance_id, "graph.pkl")
    if not os.path.exists(path):
        return {}
    try:
        graph = load_prebuilt_code_graph(prebuilt_dir, instance_id)
    except Exception:  # noqa: BLE001 — a missing/corrupt graph just disables this
        return {}
    g = getattr(graph, "graph", None)
    if g is None:
        return {}
    index: Dict[Any, Any] = {}
    for v in g.vs:
        attrs = v.attributes()
        file, start, end = (
            attrs.get("file"),
            attrs.get("start_line"),
            attrs.get("end_line"),
        )
        name = attrs.get("name")
        unified_name = attrs.get("unified_name")
        if file is None or start is None or end is None or not (name or unified_name):
            continue
        nf = normalize_file_path(file)
        for label in (name, unified_name):
            leaf = _leaf_symbol(label)
            if not leaf:
                continue
            key = (nf, leaf)
            if key not in index:  # first definition wins
                index[key] = (int(start) + 1, int(end) + 1)
    return index


_FANOUT_SEARCH_TOOLS = {
    "grep",
    "glob",
    "bash",
    "bm25_search",
    "embedding_search",
    "codeminer_context",
    "find_callers",
    "find_callees",
    "lsp_route",
    "lsp_definition",
    "lsp_references",
    "trace",
}


def _context_value(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _verified_preload_count(context: Sequence[Any]) -> int:
    return sum(
        1
        for item in context or []
        if _context_value(item, "source") == "preload"
        and _context_value(item, "state") == "verified"
    )


_READ_LINE_RE = re.compile(r"^\s*(\d+)\s+\|\s?(.*)$")
_IDENT_RE = r"[A-Za-z_][A-Za-z0-9_]*"
_SCOPED_TYPE_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]*)::([A-Z][A-Za-z0-9_]*)\b")
_GENERIC_DECL_LINE_RE = re.compile(
    rf"\b(?:async\s+def|def|class|func|type|var|const)\s+"
    rf"(?:\([^)]*\)\s*)?{_IDENT_RE}\b"
)
_SELECTOR_CALL_RE = re.compile(rf"(?:(\b{_IDENT_RE})\s*)?\.({_IDENT_RE})\s*\(")
_SELECTOR_RE = re.compile(rf"(?:(\b{_IDENT_RE})\s*)?\.({_IDENT_RE})\b")
_CALL_RE = re.compile(rf"(?<![:.])\b({_IDENT_RE})\s*\(")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
_CAMEL_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+")
_SYMBOL_STOPWORDS = {
    "False",
    "None",
    "True",
    "all",
    "any",
    "append",
    "bool",
    "break",
    "cap",
    "case",
    "class",
    "continue",
    "def",
    "dict",
    "elif",
    "else",
    "error",
    "except",
    "for",
    "func",
    "getattr",
    "if",
    "int",
    "isinstance",
    "len",
    "list",
    "make",
    "map",
    "max",
    "min",
    "nil",
    "new",
    "open",
    "panic",
    "print",
    "range",
    "recover",
    "return",
    "set",
    "str",
    "string",
    "struct",
    "sum",
    "super",
    "switch",
    "ctx",
    "context",
    "key",
    "tuple",
    "valu",
    "value",
    "values",
    "while",
    "with",
}
_QUERY_SYNONYMS = {
    "cached": {"cache"},
    "caching": {"cache"},
    "directory": {"dir", "loc", "location", "path"},
    "directories": {"dir", "loc", "location", "path"},
    "fetch": {"download", "retrieve"},
    "fetches": {"download", "retrieve"},
    "fetching": {"download", "retrieve"},
    "collection": {"tuple", "list", "set", "expr"},
    "collections": {"tuple", "list", "set", "expr"},
    "conversion": {"cast", "convert"},
    "conversions": {"cast", "convert"},
    "converted": {"cast", "convert"},
    "converting": {"cast", "convert"},
    "collect": {"iter", "iteration"},
    "collected": {"collect", "iter", "iteration"},
    "collecting": {"collect", "iter", "iteration"},
    "literal": {"tuple", "list", "set", "expr"},
    "literals": {"tuple", "list", "set", "expr"},
    "location": {"loc", "path"},
    "locations": {"loc", "path"},
    "ordered": {"tuple", "list", "set", "expr"},
    "placeholder": {"replace", "replacer", "replacement", "template"},
    "placeholders": {"replace", "replacer", "replacement", "template"},
    "property": {"provider", "value"},
    "properties": {"provider", "value"},
    "sequence": {"tuple", "list", "set", "expr"},
    "sequences": {"tuple", "list", "set", "expr"},
    "retrieval": {"retrieve", "download", "cache"},
    "retrieve": {"download", "cache"},
    "retrieves": {"download", "cache"},
    "retrieving": {"download", "cache"},
    "token": {"replace", "replacer", "replacement", "template"},
    "tokens": {"replace", "replacer", "replacement", "template"},
    "substitution": {"replace", "replacer", "replacement"},
    "substitute": {"replace", "replacer", "replacement"},
    "trigger": {"action", "queue", "run"},
    "triggered": {"action", "queue", "run"},
    "triggers": {"action", "queue", "run"},
    "resolution": {"resolve", "resolver", "replace", "replacer"},
    "resolved": {"resolve", "resolver", "replace", "replacer"},
    "variable": {"replace", "replacer", "replacement"},
    "variables": {"replace", "replacer", "replacement"},
    "wrap": {"allocation", "cast"},
    "wrapped": {"allocation", "cast"},
    "wrapping": {"allocation", "cast"},
    "inefficient": {"unnecessary", "allocation"},
    "hostname": {"host"},
    "timestamps": {"time", "timestamp"},
}
_ROUTE_ENDPOINT_TERMS = {
    "active",
    "auto",
    "caller",
    "check",
    "detect",
    "endpoint",
    "handler",
    "health",
    "iteration",
    "loop",
    "match",
    "open",
    "rule",
    "target",
}
_ROUTE_BRIDGE_TERMS = {
    "download",
    "factory",
    "fetch",
    "open",
    "replace",
    "replacement",
    "replacer",
    "resolve",
    "resolver",
    "retrieve",
    "substitution",
    "template",
    "variable",
}
_ROUTE_BRIDGE_READ_OBJECT_TERMS = {
    "placeholder",
    "substitution",
    "template",
    "token",
    "tokens",
    "variable",
}
_ROUTE_BRIDGE_READ_PATH_TERMS = {
    "bridge",
    "bridges",
    "replace",
    "replacement",
    "replacer",
    "resolve",
    "resolver",
}
_ROUTE_PROVIDER_TERMS = {
    "cache",
    "default",
    "dir",
    "loc",
    "location",
    "path",
    "provider",
    "property",
    "replacement",
    "system",
    "url",
    "value",
}
_ROUTE_TYPE_TERMS = {
    "ast",
    "enum",
    "expr",
    "list",
    "node",
    "nodes",
    "set",
    "stmt",
    "tuple",
    "type",
    "variant",
}
_ROUTE_TYPE_QUERY_TERMS = {
    "call",
    "collection",
    "collections",
    "dict",
    "expression",
    "expressions",
    "literal",
    "literals",
    "list",
    "name",
    "ordered",
    "sequence",
    "sequences",
    "set",
    "tuple",
}
_ROUTE_TYPE_VARIANT_TERMS = {"call", "dict", "list", "name", "set", "tuple"}
_ROUTE_EXPR_VARIANTS_BY_TERM = {
    "call": "ExprCall",
    "dict": "ExprDict",
    "list": "ExprList",
    "name": "ExprName",
    "set": "ExprSet",
    "tuple": "ExprTuple",
}
_SYMBOL_HINT_STOPWORDS = {
    "as_deref",
    "as_mut",
    "as_name_expr",
    "as_ref",
    "download",
    "file",
    "files",
    "is_none",
    "is_some",
    "is_some_and",
    "line",
    "lines",
    "seconds",
}


def _term_variants(term: str) -> set[str]:
    term = term.lower()
    variants = {term}
    for suffix in ("ing", "ers", "er", "ments", "ment", "es", "s"):
        if len(term) > len(suffix) + 2 and term.endswith(suffix):
            variants.add(term[: -len(suffix)])
    return variants


def _symbol_terms(text: str) -> set[str]:
    terms: set[str] = set()
    for word in _WORD_RE.findall(text or ""):
        for part in re.split(r"[_\W]+", word):
            if not part:
                continue
            pieces = _CAMEL_RE.findall(part) or [part]
            for piece in pieces:
                if len(piece) >= 3:
                    terms.update(_term_variants(piece))
    return {term for term in terms if term not in _SYMBOL_STOPWORDS}


def _route_text_terms(text: str) -> set[str]:
    terms = _symbol_terms(text)
    for raw in re.findall(r"[A-Za-z]+", (text or "").lower()):
        if raw in _ROUTE_TYPE_QUERY_TERMS:
            terms.add(raw)
    return terms


def _query_terms(query: Optional[str]) -> set[str]:
    terms = _symbol_terms(query or "")
    expanded = set(terms)
    for raw in re.findall(r"[A-Za-z]+", (query or "").lower()):
        if raw in _ROUTE_TYPE_QUERY_TERMS:
            expanded.add(raw)
    for term in terms:
        expanded.update(_QUERY_SYNONYMS.get(term, set()))
    for term in list(expanded):
        expanded.update(_QUERY_SYNONYMS.get(term, set()))
    return expanded


def _is_traceback_route_query(query_terms: set[str]) -> bool:
    if not query_terms:
        return False
    direction_terms = {
        "before",
        "caller",
        "flow",
        "originate",
        "origin",
        "where",
    }
    path_direction_terms = {"before", "caller", "flow", "originate", "origin", "where"}
    trigger_terms = {
        "accumulat",
        "accumulate",
        "assemble",
        "assembled",
        "collect",
        "collected",
        "config",
        "configuration",
        "construct",
        "construction",
        "fall",
        "fallback",
        "initialization",
        "process",
        "processing",
        "recovery",
        "trigger",
        "trigg",
        "validat",
        "validation",
    }
    has_direction = bool(query_terms & direction_terms) or bool(
        "path" in query_terms and query_terms & path_direction_terms
    )
    matched_triggers = query_terms & trigger_terms
    strong_triggers = matched_triggers - {"config", "configuration"}
    if (
        has_direction
        and matched_triggers
        and not strong_triggers
        and query_terms & (_ROUTE_BRIDGE_TERMS | _ROUTE_PROVIDER_TERMS)
    ):
        return False
    return bool(has_direction and query_terms & trigger_terms)


def _is_bridge_read_route_query(query_terms: set[str]) -> bool:
    if not query_terms:
        return False
    return bool(
        query_terms & _ROUTE_BRIDGE_READ_OBJECT_TERMS
        and query_terms & _ROUTE_BRIDGE_READ_PATH_TERMS
    )


def _augment_query_expr_variant_hints(
    symbols: Sequence[str], query_terms: set[str]
) -> List[str]:
    """Add query-requested Expr variants once a read proves the Expr family."""
    if not symbols or not query_terms:
        return list(symbols)
    has_expr_family = any(
        symbol == "Expr" or str(symbol).startswith("Expr") for symbol in symbols
    )
    if not has_expr_family:
        return list(symbols)
    out = list(symbols)
    seen = set(out)
    insert_at = out.index("Expr") + 1 if "Expr" in out else len(out)
    additions = [
        _ROUTE_EXPR_VARIANTS_BY_TERM[term]
        for term in ("tuple", "list", "set", "dict", "call", "name")
        if term in query_terms
    ]
    for symbol in reversed(additions):
        if symbol in seen:
            continue
        out.insert(insert_at, symbol)
        seen.add(symbol)
    return out


def _skip_symbol_hint(symbol: str) -> bool:
    return (
        not symbol
        or symbol in _SYMBOL_STOPWORDS
        or symbol in _SYMBOL_HINT_STOPWORDS
        or "[" in symbol
        or "]" in symbol
    )


def read_symbol_hints(
    read_outputs: Sequence[Dict[str, Any]],
    *,
    max_symbols: int = 8,
    query: Optional[str] = None,
) -> List[str]:
    """Extract LSP-definition symbol hints from formatted read outputs.

    The extractor stays conservative: Rust-style ``Type::Variant`` reads map to
    likely generated/AST leaves like ``TypeVariant`` and the base type, while
    Python/Go-style reads contribute call/selector leaves. Declaration lines are
    intentionally ignored for generic symbols: the scheduler should jump from a
    call site to a definition, not re-inject definitions the agent is already
    reading. The graph lookup downstream filters these hints to real local nodes.
    """
    scored: Dict[str, Tuple[int, int]] = {}
    all_base_variants: Dict[str, set[str]] = {}
    query_terms = _query_terms(query)
    replacement_bridge_query = bool(
        query_terms
        & {
            "placeholder",
            "token",
            "replace",
            "replacer",
            "replacement",
            "template",
            "variable",
            "resolve",
            "resolver",
        }
    )
    order = 0

    def _symbol_bonus(symbol: str) -> int:
        bonus = 0
        if "_" in symbol:
            bonus += 3
        if any(ch.isupper() for ch in symbol[1:]):
            bonus += 2
        if symbol.startswith("_"):
            bonus += 1
        return bonus

    def _add(symbol: str, score: int) -> None:
        nonlocal order
        if _skip_symbol_hint(symbol):
            return
        if symbol.startswith("__") and symbol.endswith("__"):
            return
        lower_symbol = symbol.lower()
        score += _symbol_bonus(symbol)
        if symbol.endswith(("ContextKey", "CtxKey")) or symbol.endswith("Key"):
            score -= 4
        if query_terms:
            symbol_terms = _route_text_terms(symbol)
            if (
                replacement_bridge_query
                and not symbol.endswith(("ContextKey", "CtxKey", "Key"))
                and (
                    symbol.startswith("Replace")
                    or symbol.startswith("NewReplacer")
                    or symbol_terms & {"replace", "replacer"}
                )
            ):
                score += 6
            overlap = symbol_terms & query_terms
            if overlap:
                score += min(10, 4 * len(overlap))
            if (
                lower_symbol.startswith("is_")
                and "cache" in symbol_terms
                and query_terms & {"cache", "cached", "check", "download", "remote"}
            ):
                score += 8
            if symbol.lower() in (query or "").lower():
                score += 5
        old = scored.get(symbol)
        if old is None:
            scored[symbol] = (score, order)
            order += 1
        elif score > old[0]:
            scored[symbol] = (score, old[1])

    for read in reversed(list(read_outputs or [])):
        content = str(read.get("content") or "")
        for raw_line in content.splitlines():
            match = _READ_LINE_RE.match(raw_line)
            code = match.group(2) if match else raw_line
            code = code.split("//", 1)[0]
            code = code.split("#", 1)[0]
            matches = _SCOPED_TYPE_RE.findall(code)
            if matches:
                cluster_bonus = 8 if len(matches) >= 2 else 0
                base_variants: Dict[str, set[str]] = {}
                for base, variant in matches:
                    literal_bonus = (
                        3 if variant in {"Tuple", "List", "Set", "Dict"} else 0
                    )
                    _add(f"{base}{variant}", 10 + cluster_bonus + literal_bonus)
                    base_variants.setdefault(base, set()).add(variant)
                    all_base_variants.setdefault(base, set()).add(variant)
                for base, variants in base_variants.items():
                    multi_variant_bonus = 4 if len(variants) >= 2 else 0
                    _add(base, 9 + cluster_bonus + multi_variant_bonus)
            if _GENERIC_DECL_LINE_RE.search(code):
                continue
            for receiver, symbol in _SELECTOR_CALL_RE.findall(code):
                if "_" in symbol or any(ch.isupper() for ch in symbol[1:]):
                    score = 4 if receiver in {"self", "cls"} else 7
                    _add(symbol, score)
            for match in _SELECTOR_RE.finditer(code):
                receiver, symbol = match.group(1), match.group(2)
                if not ("_" in symbol or any(ch.isupper() for ch in symbol[1:])):
                    continue
                tail = code[match.end() :].lstrip()
                if receiver in {"self", "cls"} and tail.startswith("="):
                    continue
                score = 2 if receiver in {"self", "cls"} else 3
                _add(symbol, score)
            for symbol in _CALL_RE.findall(code):
                _add(symbol, 7)
    for base, variants in all_base_variants.items():
        if len(variants) >= 2:
            _add(base, 15 + min(6, len(variants)))
    ranked = [
        symbol
        for symbol, _ in sorted(
            scored.items(), key=lambda item: (-item[1][0], item[1][1])
        )
    ]
    return _augment_query_expr_variant_hints(ranked, query_terms)[:max_symbols]


def read_outputs_have_scoped_symbols(read_outputs: Sequence[Dict[str, Any]]) -> bool:
    """Whether read output includes high-confidence scoped type references."""
    for read in read_outputs or []:
        content = str(read.get("content") or "")
        for raw_line in content.splitlines():
            match = _READ_LINE_RE.match(raw_line)
            code = match.group(2) if match else raw_line
            code = code.split("//", 1)[0]
            code = code.split("#", 1)[0]
            if _SCOPED_TYPE_RE.search(code):
                return True
    return False


def preload_symbol_hints(
    nav: Any,
    preload_candidates: Sequence[Dict[str, Any]],
    *,
    max_symbols: int = 4,
    query: Optional[str] = None,
    line_slack: int = 12,
) -> List[str]:
    """Recover route symbol hints from retrieval spans.

    Eager preload candidates can be short spans just above a function body. For
    scheduled routing, resolve overlapping or nearby graph nodes so a wrong
    first read does not monopolize the later static-LSP route.
    """
    if max_symbols <= 0 or not preload_candidates:
        return []
    file_to_idx = getattr(nav, "file_to_idx", None)
    idx_to_span = getattr(nav, "idx_to_span", None)
    if not isinstance(file_to_idx, dict) or not isinstance(idx_to_span, dict):
        return []

    from codeminer.eval.retrieval_eval import normalize_file_path

    query_terms = _query_terms(query)
    scored: Dict[str, Tuple[int, int, int]] = {}

    def _add(symbol: str, score: int, rank: int) -> None:
        symbol = _leaf_symbol(symbol)
        if _skip_symbol_hint(symbol):
            return
        leaf_overlap = len(_route_text_terms(symbol) & query_terms)
        old = scored.get(symbol)
        if old is None or (leaf_overlap, score, -rank) > (old[0], old[1], -old[2]):
            scored[symbol] = (leaf_overlap, score, rank)

    def _gap(start: int, end: int, node_start: int, node_end: int) -> int:
        if max(start, node_start) <= min(end, node_end):
            return 0
        if end < node_start:
            return node_start - end
        return start - node_end

    for rank, candidate in enumerate(preload_candidates or []):
        for explicit in (candidate.get("name"), candidate.get("symbol")):
            if explicit:
                text = f"{candidate.get('file') or ''} {explicit}"
                score = 20 + 6 * len(_route_text_terms(text) & query_terms)
                _add(str(explicit), score, rank)

        nf = normalize_file_path(candidate.get("file"))
        if not nf:
            continue
        try:
            start = int(candidate.get("start") or candidate.get("start_line"))
            end = int(candidate.get("end") or candidate.get("end_line") or start)
        except (TypeError, ValueError):
            continue
        for idx in file_to_idx.get(nf, []):
            span = idx_to_span.get(idx)
            if not span:
                continue
            file, node_start, node_end, name = span
            gap = _gap(start, end, int(node_start), int(node_end))
            if gap > line_slack:
                continue
            leaf = _leaf_symbol(name)
            if not leaf:
                continue
            node = {
                "file": file,
                "start_line": node_start,
                "end_line": node_end,
                "name": name,
            }
            role = _scheduled_candidate_role(node, query_terms=query_terms)
            text = _candidate_text(node)
            score = 28 if gap == 0 else max(4, 22 - gap)
            score += 7 * len(_route_text_terms(leaf) & query_terms)
            score += 3 * len(_route_text_terms(text) & query_terms)
            if role in {"endpoint", "bridge", "provider"}:
                score += 5
            if role == "type" and query_terms & _ROUTE_TYPE_QUERY_TERMS:
                score += 3
            score -= min(rank, 8)
            _add(leaf, score, rank)

    return [
        symbol
        for symbol, _ in sorted(
            scored.items(),
            key=lambda item: (-item[1][0], -item[1][1], item[1][2], item[0]),
        )[:max_symbols]
    ]


def _merge_symbol_hints(
    primary: Sequence[str],
    fallback: Sequence[str],
    *,
    max_symbols: int,
) -> List[str]:
    """Merge read-derived hints with bounded retrieval fallback hints."""
    out: List[str] = []
    seen: set[str] = set()
    for symbol in list(primary or []) + list(fallback or []):
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append(symbol)
        if len(out) >= max_symbols:
            break
    return out


def _candidate_key(candidate: Dict[str, Any]) -> Tuple[str, int, int, str]:
    from codeminer.eval.retrieval_eval import normalize_file_path

    return (
        normalize_file_path(candidate.get("file")),
        int(candidate.get("start_line") or candidate.get("start") or 0),
        int(candidate.get("end_line") or candidate.get("end") or 0),
        str(candidate.get("name") or ""),
    )


def _candidate_text(candidate: Dict[str, Any]) -> str:
    return f"{candidate.get('file') or ''} {candidate.get('name') or ''}"


def _scheduled_candidate_role(
    candidate: Dict[str, Any],
    *,
    query_terms: set[str],
) -> str:
    leaf = _leaf_symbol(candidate.get("name"))
    lower_leaf = leaf.lower()
    file = str(candidate.get("file") or "").lower()
    terms = _symbol_terms(_candidate_text(candidate))
    query_overlap = terms & query_terms
    span_len = max(
        0,
        int(candidate.get("end_line") or candidate.get("end") or 0)
        - int(candidate.get("start_line") or candidate.get("start") or 0),
    )
    callable_name = "(" in str(candidate.get("name") or "") or lower_leaf.startswith(
        ("globaldefault", "_get", "is_")
    )

    if file.endswith("nodes.rs") or terms & _ROUTE_TYPE_TERMS:
        return "type"
    if span_len <= 1 and not callable_name:
        return "support"
    if (
        lower_leaf.startswith("globaldefault")
        or lower_leaf.startswith("_get")
        or (terms & _ROUTE_PROVIDER_TERMS and query_overlap)
    ):
        return "provider"
    if (
        lower_leaf.startswith("new")
        or "replace" in lower_leaf
        or (terms & _ROUTE_BRIDGE_TERMS and query_overlap)
    ):
        return "bridge"
    if _is_traceback_route_query(query_terms) and span_len > 1:
        traceback_leaf_terms = terms | _route_text_terms(leaf)
        if lower_leaf.startswith(
            (
                "add",
                "build",
                "collect",
                "compile",
                "format",
                "parse",
                "process",
                "recover",
                "run",
                "validate",
            )
        ) or traceback_leaf_terms & {"action", "iter", "queue", "recover", "reset"}:
            return "endpoint"
    if (
        "#" in str(candidate.get("name") or "")
        or lower_leaf.startswith("unnecessary")
        or terms & _ROUTE_ENDPOINT_TERMS
    ):
        return "endpoint"
    return "support"


def _scheduled_location_priority_key(
    loc: Dict[str, Any],
    *,
    original_rank: int = 0,
    type_heavy: bool = False,
) -> Tuple[int, int, int, int]:
    if type_heavy:
        role_rank = {
            "type": 0,
            "endpoint": 1,
            "bridge": 2,
            "provider": 3,
            "support": 4,
        }
    else:
        role_rank = {
            "endpoint": 0,
            "bridge": 1,
            "provider": 2,
            "type": 3,
            "support": 4,
        }
    try:
        start = int(loc.get("start_line") or 0)
        end = int(loc.get("end_line") or start)
    except (TypeError, ValueError):
        start = 0
        end = 0
    span_len = max(0, end - start + 1)
    one_line_decl = 1 if span_len <= 1 else 0
    source_rank = 0 if loc.get("source") == "route_neighbor" else 1
    return (
        one_line_decl,
        role_rank.get(str(loc.get("role") or "support"), 4),
        source_rank,
        original_rank,
    )


def _route_query_provider_priority(
    loc: Dict[str, Any],
    *,
    query_terms: set[str],
    via_leafs: set[str],
) -> Optional[Tuple[int, int, int]]:
    """Return a priority tuple for provider anchors that explain query terms."""
    if str(loc.get("source") or "") != "direct_definition":
        return None
    if str(loc.get("role") or "") != "provider":
        return None
    leaf = _leaf_symbol(loc.get("name"))
    lower_leaf = leaf.lower()
    text_terms = _route_text_terms(_candidate_text(loc))
    query_overlap = text_terms & query_terms
    if not query_overlap:
        return None
    provider_signal_terms = (
        _ROUTE_PROVIDER_TERMS
        | _ROUTE_BRIDGE_TERMS
        | {
            "remote",
            "url",
        }
    )
    if not query_overlap & provider_signal_terms:
        return None
    if lower_leaf in via_leafs:
        route_rank = 0
    elif lower_leaf.startswith(("is_", "_get")):
        route_rank = 1
    elif lower_leaf.startswith(("get_", "check_")):
        route_rank = 2
    else:
        route_rank = 3
    return (route_rank, -len(query_overlap), int(loc.get("start_line") or 0))


def _route_query_provider_count(
    locations: Sequence[Dict[str, Any]],
    *,
    query_terms: set[str],
) -> int:
    via_leafs = {
        _leaf_symbol(loc.get("via")).lower()
        for loc in locations or []
        if loc.get("source") == "route_neighbor" and loc.get("via")
    }
    return sum(
        1
        for loc in locations or []
        if _route_query_provider_priority(
            loc,
            query_terms=query_terms,
            via_leafs=via_leafs,
        )
        is not None
    )


def _rank_route_auto_open_locations(
    locations: Sequence[Dict[str, Any]],
    *,
    query_terms: set[str],
    route_traceback: bool = False,
) -> List[Dict[str, Any]]:
    """Rank route anchors for bounded source auto-open selection."""
    if route_traceback:
        return _rank_traceback_guard_locations(
            locations,
            query_terms=query_terms,
        )

    type_heavy = (
        sum(1 for loc in locations or [] if str(loc.get("role") or "") == "type") >= 2
    )
    if type_heavy:
        return [
            loc
            for _rank, loc in sorted(
                enumerate(locations or []),
                key=lambda item: _scheduled_location_priority_key(
                    item[1], original_rank=item[0], type_heavy=True
                ),
            )
        ]

    core_roles = {"endpoint", "bridge", "provider"}
    via_leafs = {
        _leaf_symbol(loc.get("via")).lower()
        for loc in locations or []
        if loc.get("source") == "route_neighbor" and loc.get("via")
    }
    protected_keys = _route_budget_must_keep_keys(locations)
    bridge_heavy = bool(query_terms & _ROUTE_BRIDGE_TERMS) or (
        sum(1 for loc in locations or [] if str(loc.get("role") or "") == "bridge") >= 2
    )

    def _key(
        item: Tuple[int, Dict[str, Any]]
    ) -> Tuple[int, int, int, int, int, int, int]:
        original_rank, loc = item
        base = _scheduled_location_priority_key(
            loc,
            original_rank=original_rank,
            type_heavy=False,
        )
        route_neighbor_core = (
            loc.get("source") == "route_neighbor"
            and str(loc.get("role") or "") in core_roles
        )
        role = str(loc.get("role") or "")
        leaf = _leaf_symbol(loc.get("name")).lower()
        query_provider_priority = _route_query_provider_priority(
            loc,
            query_terms=query_terms,
            via_leafs=via_leafs,
        )
        protected_direct_bridge = (
            bridge_heavy
            and _candidate_key(loc) in protected_keys
            and loc.get("source") == "direct_definition"
            and role in {"bridge", "provider", "type"}
        )
        route_bridge_via = (
            loc.get("source") == "direct_definition"
            and role in {"bridge", "provider", "type"}
            and leaf in via_leafs
        )
        return (
            base[0],
            (
                0
                if route_neighbor_core
                else (
                    1
                    if query_provider_priority is not None
                    else 2 if route_bridge_via or protected_direct_bridge else 3
                )
            ),
            query_provider_priority[0] if query_provider_priority is not None else 0,
            query_provider_priority[1] if query_provider_priority is not None else 0,
            base[1],
            base[2],
            base[3],
        )

    return [loc for _rank, loc in sorted(enumerate(locations or []), key=_key)]


def _scheduled_route_prompt_locations(
    locations: Sequence[Dict[str, Any]],
    *,
    query_terms: Optional[set[str]] = None,
    route_traceback: bool = False,
) -> List[Dict[str, Any]]:
    """Order route anchors for prompt injection without changing the route set."""
    raw = [dict(loc) for loc in locations or []]
    if not route_traceback:
        return raw
    return [
        dict(loc)
        for loc in _rank_traceback_guard_locations(
            raw,
            query_terms=query_terms or set(),
        )
    ]


def _scheduled_candidate_score(
    candidate: Dict[str, Any],
    *,
    query_terms: set[str],
    direct_rank: int,
) -> int:
    leaf = _leaf_symbol(candidate.get("name"))
    lower_leaf = leaf.lower()
    terms = _route_text_terms(_candidate_text(candidate))
    span_len = max(
        0,
        int(candidate.get("end_line") or candidate.get("end") or 0)
        - int(candidate.get("start_line") or candidate.get("start") or 0),
    )
    score = 0
    score += 5 * len(terms & query_terms)
    if candidate.get("source") == "direct_definition":
        score += max(0, 8 - direct_rank)
    else:
        score += 3
        if candidate.get("role") in {"endpoint", "provider"}:
            score += 5
    if candidate.get("role") in {"endpoint", "bridge", "provider"}:
        score += 4
    if lower_leaf.startswith("_"):
        score += 4
    if lower_leaf.startswith(("globaldefault", "_get")):
        score += 5
    if lower_leaf.startswith("new"):
        score += 8
    if "replace" in lower_leaf:
        score += 4
    if lower_leaf.startswith("is_") and candidate.get("role") == "provider":
        score += 6
    if (
        candidate.get("role") == "endpoint"
        and "#" in str(candidate.get("name") or "")
        and span_len > 12
        and len(terms & query_terms) >= 2
    ):
        score += 6
    if lower_leaf.startswith(("clear", "delete", "remove")) and not (
        query_terms & {"clear", "delete", "remove"}
    ):
        score -= 8
    if span_len > 220:
        score -= 4
    elif span_len <= 12:
        score += 1
    return score


def _allow_scheduled_route_neighbor(
    candidate: Dict[str, Any],
    *,
    query_terms: set[str],
) -> bool:
    file = str(candidate.get("file") or "")
    lower_file = file.lower()
    leaf = _leaf_symbol(candidate.get("name"))
    lower_leaf = leaf.lower()
    if not file or not leaf:
        return False
    if (
        "/test" in lower_file
        or "tests/" in lower_file
        or lower_file.startswith("test/")
        or lower_file.startswith("docs/")
        or lower_leaf.startswith("test")
    ):
        return False
    terms = _route_text_terms(_candidate_text(candidate))
    if terms & query_terms:
        return True
    if lower_leaf.startswith(("globaldefault", "_get")):
        return True
    if "replace" in lower_leaf and query_terms & _ROUTE_BRIDGE_TERMS:
        return True
    return False


def _annotate_scheduled_candidate(
    candidate: Dict[str, Any],
    *,
    role: str,
    source: str,
    direct_rank: int,
    query_terms: set[str],
    via: Optional[str] = None,
) -> Dict[str, Any]:
    annotated = dict(candidate)
    annotated["role"] = role
    annotated["source"] = source
    if via:
        annotated["via"] = via
    annotated["_direct_rank"] = direct_rank
    annotated["_score"] = _scheduled_candidate_score(
        annotated,
        query_terms=query_terms,
        direct_rank=direct_rank,
    )
    return annotated


def scheduled_definition_candidates_for_symbols(
    nav: Any,
    symbols: Sequence[str],
    *,
    max_nodes: int = 5,
    query: Optional[str] = None,
    pool_nodes: Optional[int] = None,
    neighbor_nodes: int = 12,
) -> List[Dict[str, Any]]:
    """Select scheduled definition anchors with route-role quotas.

    The direct static-LSP definition list is still the source of truth, but
    graph neighbours of those direct anchors can fill role gaps when they are
    query-relevant runtime code. This moves the useful top-anchor correction
    signal earlier: the model sees endpoint/bridge/provider/type anchors before
    writing the first answer instead of being corrected after a crowded answer.
    """
    if not symbols or not hasattr(nav, "definitions_for_symbols"):
        return []
    query_terms = _query_terms(query)
    pool_limit = max(max_nodes, pool_nodes or max(max_nodes * 3, max_nodes + 5))
    direct = list(nav.definitions_for_symbols(list(symbols), max_nodes=pool_limit))
    if not direct:
        return []

    raw_variant_terms = {
        raw
        for raw in re.findall(r"[A-Za-z]+", (query or "").lower())
        if raw in _ROUTE_TYPE_VARIANT_TERMS
    }
    candidates: List[Dict[str, Any]] = []
    seen: set[Tuple[str, int, int, str]] = set()

    def _add(candidate: Dict[str, Any]) -> None:
        key = _candidate_key(candidate)
        if key in seen:
            return
        seen.add(key)
        candidates.append(candidate)

    direct_ranks: Dict[Tuple[str, int, int, str], int] = {}
    direct_role_counts: Dict[str, int] = {}
    for rank, candidate in enumerate(direct):
        role = _scheduled_candidate_role(candidate, query_terms=query_terms)
        direct_role_counts[role] = direct_role_counts.get(role, 0) + 1
        direct_ranks[_candidate_key(candidate)] = rank
        _add(
            _annotate_scheduled_candidate(
                candidate,
                role=role,
                source="direct_definition",
                direct_rank=rank,
                query_terms=query_terms,
            )
        )
    type_heavy = direct_role_counts.get("type", 0) >= 3

    if query_terms and hasattr(nav, "neighbors"):
        for rank, candidate in enumerate(direct[: max(max_nodes, 8)]):
            file = candidate.get("file")
            leaf = _leaf_symbol(candidate.get("name"))
            if not file or not leaf:
                continue
            source_role = _scheduled_candidate_role(candidate, query_terms=query_terms)
            if type_heavy and source_role == "type":
                continue
            try:
                neighbours = nav.neighbors([(file, leaf)], max_nodes=neighbor_nodes)
            except Exception:  # noqa: BLE001 - neighbour expansion is opportunistic
                neighbours = []
            for neighbour in neighbours or []:
                if not _allow_scheduled_route_neighbor(
                    neighbour,
                    query_terms=query_terms,
                ):
                    continue
                role = _scheduled_candidate_role(neighbour, query_terms=query_terms)
                if role not in {"endpoint", "provider"}:
                    continue
                neighbour_leaf = _leaf_symbol(neighbour.get("name")).lower()
                neighbour_terms = _route_text_terms(_candidate_text(neighbour))
                neighbour_span_len = max(
                    0,
                    int(neighbour.get("end_line") or neighbour.get("end") or 0)
                    - int(neighbour.get("start_line") or neighbour.get("start") or 0),
                )
                if role == "endpoint" and (
                    neighbour_leaf.startswith("_") or neighbour_span_len <= 1
                ):
                    continue
                if role == "provider" and not (
                    neighbour_leaf.startswith(("globaldefault", "_get"))
                    or (
                        neighbour_leaf.startswith("get")
                        and neighbour_terms & _ROUTE_PROVIDER_TERMS
                        and neighbour_terms & query_terms
                    )
                ):
                    continue
                if (
                    role == "provider"
                    and _candidate_key(neighbour)[0] == _candidate_key(candidate)[0]
                    and not neighbour_leaf.startswith(("globaldefault", "_get"))
                ):
                    continue
                _add(
                    _annotate_scheduled_candidate(
                        neighbour,
                        role=role,
                        source="route_neighbor",
                        direct_rank=rank + len(direct),
                        query_terms=query_terms,
                        via=leaf,
                    )
                )

    buckets: Dict[str, List[Dict[str, Any]]] = {
        "endpoint": [],
        "bridge": [],
        "provider": [],
        "type": [],
        "support": [],
    }
    for candidate in candidates:
        buckets.setdefault(str(candidate.get("role") or "support"), []).append(
            candidate
        )

    _GENERAL_TYPE_QUERY_TERMS = {
        "ast",
        "collection",
        "collections",
        "expr",
        "expression",
        "expressions",
        "literal",
        "literals",
        "node",
        "nodes",
        "ordered",
        "sequence",
        "sequences",
        "type",
        "variant",
        "variants",
    }

    def _type_candidate_rank(c: Dict[str, Any]) -> int:
        file = str(c.get("file") or "").lower()
        name = str(c.get("name") or "")
        leaf = _leaf_symbol(c.get("name"))
        lower_leaf = leaf.lower()
        leaf_terms = {part.lower() for part in (_CAMEL_RE.findall(leaf) or [leaf])}
        specific_query_terms = query_terms - _GENERAL_TYPE_QUERY_TERMS
        preferred_ast_nodes_file = "ruff_python_ast/src/nodes.rs" in file
        if "#" in name or file.endswith("/node.rs") or file.endswith("node.rs"):
            return 4
        if preferred_ast_nodes_file and lower_leaf in {"expr", "stmt"}:
            return 0
        if lower_leaf in {"expr", "stmt"} and not preferred_ast_nodes_file:
            return 4
        if (
            preferred_ast_nodes_file
            and lower_leaf.startswith(("expr", "stmt"))
            and leaf_terms & specific_query_terms & _ROUTE_TYPE_VARIANT_TERMS
        ):
            return 1
        if preferred_ast_nodes_file and lower_leaf.startswith(("expr", "stmt")):
            return 2
        if preferred_ast_nodes_file:
            return 3
        return 4

    def _candidate_sort_key(c: Dict[str, Any]) -> Tuple[int, int, int, int, str, int]:
        leaf = _leaf_symbol(c.get("name")).lower()
        source_rank = 0 if c.get("source") == "direct_definition" else 1
        if (
            c.get("role") == "provider"
            and c.get("source") == "route_neighbor"
            and leaf.startswith(("globaldefault", "_get"))
        ):
            source_rank = -1
        type_rank = _type_candidate_rank(c) if c.get("role") == "type" else 0
        return (
            source_rank,
            type_rank,
            -int(c.get("_score") or 0),
            int(c.get("_direct_rank") or 0),
            str(c.get("file") or ""),
            int(c.get("start_line") or 0),
        )

    for bucket in buckets.values():
        bucket.sort(key=_candidate_sort_key)

    def _query_overlap_count(c: Dict[str, Any]) -> int:
        return len(_route_text_terms(_candidate_text(c)) & query_terms)

    def _leaf_query_overlap_count(c: Dict[str, Any]) -> int:
        return len(_route_text_terms(_leaf_symbol(c.get("name"))) & query_terms)

    reserved_type_heavy_route = None
    if type_heavy and query_terms:
        high_signal_routes = [
            c
            for c in candidates
            if c.get("source") == "direct_definition"
            and c.get("role") in {"endpoint", "bridge", "provider"}
            and (c.get("role") == "endpoint" or _query_overlap_count(c) >= 2)
        ]
        high_signal_routes.sort(
            key=lambda c: (
                -_leaf_query_overlap_count(c),
                -_query_overlap_count(c),
                -int(c.get("_score") or 0),
                int(c.get("_direct_rank") or 0),
            )
        )
        reserved_type_heavy_route = (
            high_signal_routes[0] if high_signal_routes else None
        )

    selected: List[Dict[str, Any]] = []
    selected_keys: set[Tuple[str, int, int, str]] = set()

    def _take(candidate: Dict[str, Any]) -> None:
        if len(selected) >= max_nodes:
            return
        key = _candidate_key(candidate)
        if key in selected_keys:
            return
        selected_keys.add(key)
        cleaned = {
            k: v
            for k, v in candidate.items()
            if not str(k).startswith("_") and v is not None
        }
        selected.append(cleaned)

    def _is_weak_type_candidate(candidate: Dict[str, Any]) -> bool:
        raw_name = str(candidate.get("name") or "")
        raw_file = str(candidate.get("file") or "").lower()
        return (
            "#" in raw_name
            or "ruff_python_semantic" in raw_file
            or raw_file.endswith("/node.rs")
            or raw_file.endswith("node.rs")
            or _type_candidate_rank(candidate) >= 4
        )

    def _thin_type_candidates(limit: int) -> List[Dict[str, Any]]:
        if limit <= 0:
            return []
        selected_types: List[Dict[str, Any]] = []
        selected_type_keys: set[Tuple[str, int, int, str]] = set()

        def _add_type(candidate: Dict[str, Any]) -> None:
            if len(selected_types) >= limit:
                return
            if _is_weak_type_candidate(candidate):
                return
            key = _candidate_key(candidate)
            if key in selected_type_keys:
                return
            selected_type_keys.add(key)
            selected_types.append(candidate)

        for candidate in buckets["type"]:
            if _type_candidate_rank(candidate) == 0:
                _add_type(candidate)
                break

        variant_capacity = max(0, limit - len(selected_types))
        if len(raw_variant_terms) >= 3:
            variant_limit = min(3, variant_capacity)
        else:
            variant_limit = min(2, variant_capacity)
        for candidate in buckets["type"]:
            if len(selected_types) >= limit or variant_limit <= 0:
                break
            if _type_candidate_rank(candidate) != 1:
                continue
            _add_type(candidate)
            variant_limit -= 1

        return selected_types

    if all(c.get("role") == "type" for c in candidates[:max_nodes]):
        type_candidates = _thin_type_candidates(
            max(0, max_nodes - 1 if reserved_type_heavy_route else max_nodes)
        )
        if reserved_type_heavy_route:
            for candidate in type_candidates:
                _take(candidate)
            _take(reserved_type_heavy_route)
            return selected
        for candidate in type_candidates:
            _take(candidate)
        return selected

    if type_heavy:
        type_limit = max_nodes - 1 if reserved_type_heavy_route else max_nodes
        for candidate in _thin_type_candidates(type_limit):
            _take(candidate)
        if reserved_type_heavy_route:
            _take(reserved_type_heavy_route)
            return selected
        if len(selected) >= max_nodes:
            return selected

    role_order = ["endpoint", "bridge", "provider", "type"]
    if buckets["type"] and not (
        buckets["endpoint"] or buckets["bridge"] or buckets["provider"]
    ):
        role_order = ["type", "endpoint", "bridge", "provider"]

    for role in role_order:
        if buckets.get(role):
            _take(buckets[role][0])
    rest_role_priority = {
        "endpoint": 0,
        "bridge": 0,
        "provider": 0,
        "type": 1,
        "support": 2,
    }

    def _rest_source_rank(c: Dict[str, Any]) -> int:
        leaf = _leaf_symbol(c.get("name")).lower()
        if (
            c.get("role") == "provider"
            and c.get("source") == "route_neighbor"
            and leaf.startswith(("globaldefault", "_get"))
        ):
            return -1
        return 0 if c.get("source") == "direct_definition" else 1

    rest = sorted(
        candidates,
        key=lambda c: (
            rest_role_priority.get(str(c.get("role") or "support"), 2),
            _rest_source_rank(c),
            -int(c.get("_score") or 0),
            int(c.get("_direct_rank") or 0),
            str(c.get("file") or ""),
            int(c.get("start_line") or 0),
        ),
    )
    for candidate in rest:
        _take(candidate)
        if len(selected) >= max_nodes:
            break
    return selected


def tool_call_pressure(
    tool_calls: Sequence[Any],
    *,
    turns: int,
    max_turns: int,
    min_search_calls: int = 4,
    min_read_calls: Optional[int] = None,
    turn_ratio: float = 0.75,
) -> Dict[str, Any]:
    """Return trace-derived search pressure from raw tool calls."""
    search_calls = sum(
        1 for tc in tool_calls if getattr(tc, "skill_id", None) in _FANOUT_SEARCH_TOOLS
    )
    read_calls = sum(1 for tc in tool_calls if getattr(tc, "skill_id", None) == "read")
    near_budget = bool(max_turns and turns >= max(1, int(max_turns * turn_ratio)))
    high_search = search_calls >= min_search_calls
    high_read = bool(min_read_calls and read_calls >= min_read_calls)
    reasons = []
    if high_search:
        reasons.append("search_calls")
    if high_read:
        reasons.append("read_calls")
    if near_budget:
        reasons.append("turn_budget")
    triggered = bool(reasons)
    reason = "+".join(reasons) if reasons else "none"
    return {
        "triggered": triggered,
        "reason": reason,
        "search_calls": search_calls,
        "read_calls": read_calls,
        "turns": turns,
        "max_turns": max_turns,
        "min_search_calls": min_search_calls,
        "min_read_calls": min_read_calls,
        "turn_ratio": turn_ratio,
    }


def search_fanout_pressure(
    result: Any,
    *,
    max_turns: int,
    min_search_calls: int = 4,
    min_read_calls: Optional[int] = None,
    turn_ratio: float = 0.75,
) -> Dict[str, Any]:
    """Return trace-derived search pressure for proactive graph expansion."""
    return tool_call_pressure(
        list(getattr(result, "tool_calls", []) or []),
        turns=int(getattr(result, "total_turns", 0) or 0),
        max_turns=max_turns,
        min_search_calls=min_search_calls,
        min_read_calls=min_read_calls,
        turn_ratio=turn_ratio,
    )


def _span_overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return max(a_start, b_start) <= min(a_end, b_end)


def _scheduled_context_locations(
    result: Any, *, reasons: set[str]
) -> List[Dict[str, Any]]:
    trace = getattr(result, "trace", None)
    events = getattr(trace, "events", None) or []
    locations: List[Dict[str, Any]] = []
    seen: set[Tuple[str, int, int, str]] = set()
    for event in events:
        if getattr(event, "kind", None) != "scheduled_context":
            continue
        data = getattr(event, "data", None) or {}
        if data.get("source") != "scheduled_graph_lsp":
            continue
        if str(data.get("reason") or "") not in reasons:
            continue
        for loc in data.get("locations") or []:
            file = str(loc.get("file") or "")
            try:
                start = int(loc.get("start_line"))
                end = int(loc.get("end_line") or start)
            except (TypeError, ValueError):
                continue
            if not file or start <= 0 or end <= 0:
                continue
            name = str(loc.get("name") or "")
            key = (file, start, end, name)
            if key in seen:
                continue
            seen.add(key)
            locations.append(
                {
                    "file": file,
                    "start_line": start,
                    "end_line": end,
                    "name": name,
                    "role": loc.get("role"),
                    "source": loc.get("source"),
                    "via": loc.get("via"),
                }
            )
    return locations


def _scheduled_definition_locations(result: Any) -> List[Dict[str, Any]]:
    return _scheduled_context_locations(result, reasons={"read_symbols"})


def _scheduled_final_guard_locations(result: Any) -> List[Dict[str, Any]]:
    return _scheduled_context_locations(result, reasons={"route_finalization_guard"})


def _scheduled_final_guard_priority_count(result: Any) -> Optional[int]:
    trace = getattr(result, "trace", None)
    events = getattr(trace, "events", None) or []
    for event in reversed(events):
        if getattr(event, "kind", None) != "scheduled_context":
            continue
        data = getattr(event, "data", None) or {}
        if data.get("source") != "scheduled_graph_lsp":
            continue
        if str(data.get("reason") or "") != "route_finalization_guard":
            continue
        try:
            priority_count = int(data.get("priority_count"))
        except (TypeError, ValueError):
            return None
        return priority_count if priority_count > 0 else None
    return None


def _tool_call_turns_by_id(result: Any) -> Dict[str, int]:
    """Return trace turn numbers for tool calls, keyed by tool call id."""
    trace = getattr(result, "trace", None)
    events = getattr(trace, "events", None) or []
    turns: Dict[str, int] = {}
    for event in events:
        if getattr(event, "kind", None) != "tool_call":
            continue
        data = getattr(event, "data", None) or {}
        tool_call_id = str(data.get("tool_call_id") or "")
        if not tool_call_id:
            continue
        try:
            turn = int(event.turn)
        except (AttributeError, TypeError, ValueError):
            continue
        turns.setdefault(tool_call_id, turn)
    return turns


def _read_confirmed_scheduled_locations(
    result: Any, locations: Sequence[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    from codeminer.eval.retrieval_eval import normalize_file_path

    reads: List[Tuple[str, int, int]] = []
    trace = getattr(result, "trace", None)

    def _append_read_range(args: Any) -> None:
        if not isinstance(args, dict):
            return
        file = normalize_file_path(args.get("file_path"))
        if not file:
            return
        try:
            start = int(args.get("offset") or 1)
        except (TypeError, ValueError):
            start = 1
        try:
            limit = int(args.get("limit") or 0)
        except (TypeError, ValueError):
            limit = 0
        end = start + limit - 1 if limit > 0 else 10**12
        reads.append((file, start, end))

    for tc in getattr(result, "tool_calls", []) or []:
        if getattr(tc, "skill_id", None) != "read" or getattr(tc, "error", None):
            continue
        _append_read_range(getattr(tc, "arguments", None) or {})
    for event in getattr(trace, "events", None) or []:
        if getattr(event, "kind", None) != "tool_call":
            continue
        data = getattr(event, "data", None) or {}
        if str(data.get("tool") or "") != "read" or data.get("error"):
            continue
        _append_read_range(data.get("arguments") or {})
    for event in getattr(trace, "events", None) or []:
        if getattr(event, "kind", None) != "scheduled_context":
            continue
        data = getattr(event, "data", None) or {}
        if data.get("source") != "scheduled_graph_lsp":
            continue
        for loc in data.get("auto_open_locations") or []:
            file = normalize_file_path(loc.get("file"))
            if not file:
                continue
            try:
                start = int(loc.get("start_line"))
                end = int(loc.get("end_line") or start)
            except (TypeError, ValueError):
                continue
            reads.append((file, start, end))

    confirmed: List[Dict[str, Any]] = []
    for loc in locations or []:
        file = normalize_file_path(loc.get("file"))
        if not file:
            continue
        try:
            start = int(loc.get("start_line"))
            end = int(loc.get("end_line") or start)
        except (TypeError, ValueError):
            continue
        if any(
            rfile == file and _span_overlaps(start, end, rstart, rend)
            for rfile, rstart, rend in reads
        ):
            confirmed.append(dict(loc))
    return confirmed


def _scheduled_top_anchor_priority_count(
    locations: Sequence[Dict[str, Any]],
    configured: Optional[int] = None,
) -> int:
    if configured is not None and configured > 0:
        return configured
    type_count = sum(1 for loc in locations if str(loc.get("role") or "") == "type")
    if type_count >= 2:
        return min(2, type_count)
    return 1


def scheduled_anchor_audit(
    result: Any, *, repo_path: Optional[str] = None
) -> Dict[str, Any]:
    """Audit whether read-confirmed scheduled definitions reached the answer."""
    from codeminer.eval.retrieval_eval import normalize_file_path, parse_answer_spans

    offered = _scheduled_definition_locations(result)
    read_confirmed = _read_confirmed_scheduled_locations(result, offered)
    answer_spans = parse_answer_spans(getattr(result, "answer", "") or "", repo_path)
    cited: List[Dict[str, Any]] = []
    for loc in read_confirmed:
        file = normalize_file_path(loc.get("file"))
        try:
            start = int(loc.get("start_line"))
            end = int(loc.get("end_line") or start)
        except (TypeError, ValueError):
            continue
        if any(
            normalize_file_path(span.get("file")) == file
            and _span_overlaps(
                start,
                end,
                int(span.get("start") or span.get("start_line") or 0),
                int(span.get("end") or span.get("end_line") or 0),
            )
            for span in answer_spans
        ):
            cited.append(dict(loc))
    triggered = bool(read_confirmed and not cited)
    return {
        "triggered": triggered,
        "offered_count": len(offered),
        "read_count": len(read_confirmed),
        "cited_count": len(cited),
        "read_uncited": [] if not triggered else read_confirmed,
    }


def scheduled_answer_ordering_audit(
    result: Any, *, repo_path: Optional[str] = None, top_k: int = 5
) -> Dict[str, Any]:
    """Detect when helper ranges crowd core locations out of the top answer spans.

    The scoring harness evaluates the first few parsed answer spans. A model can
    find the right route but mention many internal helpers with exact line
    ranges before the real endpoint/provider locations. This audit is
    intentionally GT-free: it looks for a narrow shape that indicates answer
    ordering drift after scheduled graph/LSP context was injected.
    """
    from collections import Counter

    from codeminer.eval.retrieval_eval import normalize_file_path, parse_answer_spans

    if top_k <= 0:
        top_k = 5
    answer_spans = parse_answer_spans(getattr(result, "answer", "") or "", repo_path)
    if len(answer_spans) <= top_k:
        return {
            "triggered": False,
            "reason": "short_answer",
            "answer_span_count": len(answer_spans),
            "top_k": top_k,
            "dominant_file": None,
            "dominant_file_count": 0,
            "short_top_count": 0,
            "tail_candidates": [],
        }

    def _span_len(span: Dict[str, Any]) -> int:
        try:
            start = int(span.get("start") or span.get("start_line") or 0)
            end = int(span.get("end") or span.get("end_line") or start)
        except (TypeError, ValueError):
            return 0
        return max(0, end - start + 1)

    top = answer_spans[:top_k]
    tail = answer_spans[top_k:]
    top_files = [normalize_file_path(span.get("file")) for span in top]
    top_files = [file for file in top_files if file]
    counts = Counter(top_files)
    dominant_file, dominant_count = (None, 0)
    if counts:
        dominant_file, dominant_count = counts.most_common(1)[0]
    short_top_count = sum(
        1 for span in top if _span_len(span) and _span_len(span) <= 12
    )
    tail_different_file = [
        span
        for span in tail
        if dominant_file and normalize_file_path(span.get("file")) != dominant_file
    ]
    long_tail = [span for span in tail if _span_len(span) >= 20]

    reason = "none"
    triggered = False
    if dominant_count >= max(3, top_k - 1) and tail_different_file:
        reason = "dominant_file_top_spans"
        triggered = True
    elif short_top_count >= 3 and long_tail:
        reason = "helper_spans_before_long_context"
        triggered = True

    tail_candidates = []
    if triggered:
        # Offer a small, ordered reminder of locations already present later in
        # the answer; the correction should reprioritize, not discover anew.
        for span in tail:
            if len(tail_candidates) >= top_k:
                break
            file = normalize_file_path(span.get("file"))
            if not file:
                continue
            try:
                start = int(span.get("start") or span.get("start_line"))
                end = int(span.get("end") or span.get("end_line") or start)
            except (TypeError, ValueError):
                continue
            tail_candidates.append(
                {
                    "file": file,
                    "start_line": start,
                    "end_line": end,
                    "name": str(span.get("name") or ""),
                }
            )

    return {
        "triggered": triggered,
        "reason": reason,
        "answer_span_count": len(answer_spans),
        "top_k": top_k,
        "dominant_file": dominant_file,
        "dominant_file_count": dominant_count,
        "short_top_count": short_top_count,
        "tail_candidates": tail_candidates,
    }


def scheduled_top_anchor_audit(
    result: Any,
    *,
    repo_path: Optional[str] = None,
    top_k: int = 5,
    priority_count: int = 1,
) -> Dict[str, Any]:
    """Audit whether the highest-priority scheduled anchors made answer top-k."""
    from codeminer.eval.retrieval_eval import normalize_file_path, parse_answer_spans

    if top_k <= 0:
        top_k = 5
    if priority_count <= 0:
        priority_count = 1
    offered = _scheduled_definition_locations(result)
    final_guard = _scheduled_final_guard_locations(result)
    priority_source = final_guard or offered
    if not final_guard and any(loc.get("role") is not None for loc in priority_source):
        # Provider/bridge routes are useful context, but making them mandatory
        # top-anchor corrections caused costly full retries on Caddy bridge
        # traces. Keep the correction contract to endpoint/type anchors unless
        # a final guard explicitly protects a broader route set.
        priority_source = [
            loc
            for loc in priority_source
            if str(loc.get("role") or "") in {"endpoint", "type"}
        ]
    if final_guard:
        final_guard_priority_count = _scheduled_final_guard_priority_count(result)
        final_guard_protected = (
            final_guard_priority_count
            if final_guard_priority_count is not None
            else len(final_guard)
        )
        priority_count = max(priority_count, min(top_k, final_guard_protected))
    read_scope = _merge_guard_locations(
        offered,
        final_guard,
        max_locations=len(offered) + len(final_guard),
    )
    read_confirmed = _read_confirmed_scheduled_locations(result, read_scope)
    answer_spans = parse_answer_spans(getattr(result, "answer", "") or "", repo_path)
    top_spans = answer_spans[:top_k]
    if not priority_source:
        return {
            "triggered": False,
            "reason": "no_priority_endpoint_or_type",
            "offered_count": len(offered),
            "read_count": len(read_confirmed),
            "answer_span_count": len(answer_spans),
            "top_k": top_k,
            "priority_count": 0,
            "missing_count": 0,
            "missing_unread_count": 0,
            "missing_anchors": [],
            "read_confirmed": [dict(loc) for loc in read_confirmed],
        }

    def _overlaps_answer(loc: Dict[str, Any], spans: Sequence[Dict[str, Any]]) -> bool:
        file = normalize_file_path(loc.get("file"))
        if not file:
            return False
        try:
            start = int(loc.get("start_line"))
            end = int(loc.get("end_line") or start)
        except (TypeError, ValueError):
            return False
        for span in spans:
            try:
                span_start = int(span.get("start") or span.get("start_line") or 0)
                span_end = int(span.get("end") or span.get("end_line") or span_start)
            except (TypeError, ValueError):
                continue
            if normalize_file_path(span.get("file")) == file and _span_overlaps(
                start, end, span_start, span_end
            ):
                return True
        return False

    read_keys = {_candidate_key(loc) for loc in read_confirmed}

    type_heavy = (
        sum(1 for loc in priority_source if str(loc.get("role") or "") == "type") >= 2
    )
    ranked_priority = sorted(
        enumerate(priority_source),
        key=lambda item: _scheduled_location_priority_key(
            item[1], original_rank=item[0], type_heavy=type_heavy
        ),
    )
    ranked_priority = [loc for _rank, loc in ranked_priority]
    priority = [dict(loc) for loc in ranked_priority[:priority_count]]
    missing = [loc for loc in priority if not _overlaps_answer(loc, top_spans)]
    missing_unread = [loc for loc in missing if _candidate_key(loc) not in read_keys]
    triggered = bool(answer_spans and missing)
    reason = "none"
    if triggered and missing_unread:
        reason = "priority_anchor_unread"
    elif triggered:
        reason = "priority_anchor_outside_top_k"
    return {
        "triggered": triggered,
        "reason": reason,
        "offered_count": len(offered),
        "read_count": len(read_confirmed),
        "answer_span_count": len(answer_spans),
        "top_k": top_k,
        "priority_count": priority_count,
        "missing_count": len(missing),
        "missing_unread_count": len(missing_unread),
        "missing_anchors": missing,
        "read_confirmed": [dict(loc) for loc in read_confirmed],
    }


def render_scheduled_anchor_correction(candidates: Sequence[Dict[str, Any]]) -> str:
    """Prompt preamble for one bounded answer-anchor correction pass."""
    if not candidates:
        return ""
    lines = [
        "Scheduled graph/LSP answer audit: you read the definition anchors below, "
        "but your final answer did not cite any of their exact ranges. Re-answer "
        "the original localization request. If one of these definitions explains "
        "the route, include its exact range in the final single-line, "
        "comma-separated Locations line. Use plain Files:/Symbols:/Locations: "
        "contract lines, not markdown bullets:",
    ]
    for c in candidates:
        nm = c.get("name") or ""
        lines.append(f"- {c['file']}:{c['start_line']}-{c['end_line']} {nm}".rstrip())
    return "\n".join(lines)


def render_scheduled_top_anchor_correction(audit: Dict[str, Any]) -> str:
    """Prompt preamble for top-k scheduled-anchor correction."""
    candidates = audit.get("missing_anchors") or []
    if not audit.get("triggered") or not candidates:
        return ""
    top_k = int(audit.get("top_k") or 5)
    if audit.get("reason") == "priority_anchor_unread":
        first_sentence = (
            "Scheduled graph/LSP top-anchor audit: a high-priority definition "
            "anchor was offered but was not read before the final answer."
        )
        instruction = (
            "Read the anchor below first, then re-answer the original "
            "localization request with a compact, single-line Locations "
            "shortlist ordered by route importance."
        )
    else:
        first_sentence = (
            "Scheduled graph/LSP top-anchor audit: a high-priority definition "
            f"anchor that you read did not appear in the first {top_k} parsed "
            "answer ranges."
        )
        instruction = (
            "Re-answer the original localization request with a compact, "
            "single-line Locations shortlist ordered by route importance. "
            "The required anchor below is read-confirmed route evidence; put it "
            "in the final Locations line before lower-priority helper or "
            "interior ranges unless you explicitly explain why it is unrelated."
        )
    lines = [
        first_sentence,
        instruction,
        f"Put at most {top_k} comma-separated entries in the final Locations line. "
        "Use plain Files:/Symbols:/Locations: contract lines, not markdown "
        "bullets. Required anchors below must appear before optional anchors; "
        "do not let wrapper/helper ranges or smaller interior ranges appear "
        "before them:",
    ]
    lines.append("Required top-route anchors:")
    for c in candidates:
        nm = c.get("name") or ""
        lines.append(f"- {c['file']}:{c['start_line']}-{c['end_line']} {nm}".rstrip())
    other = audit.get("read_confirmed") or []
    remaining = [
        c
        for c in other
        if not any(
            c.get("file") == m.get("file")
            and c.get("start_line") == m.get("start_line")
            and c.get("end_line") == m.get("end_line")
            for m in candidates
        )
    ]
    if remaining:
        lines.append("Other read scheduled anchors to consider after required anchors:")
        for c in remaining[: max(0, top_k - len(candidates))]:
            nm = c.get("name") or ""
            lines.append(
                f"- {c['file']}:{c['start_line']}-{c['end_line']} {nm}".rstrip()
            )
    return "\n".join(lines)


_LOCATION_ENTRY_RE = re.compile(r"(?P<file>[^,\s]+):(?P<start>\d+)(?:-(?P<end>\d+))?")
_ANSWER_PATH_ALIAS_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
}


def _contract_values(answer: str, label: str) -> List[str]:
    prefix = f"{label}:"
    for line in (answer or "").splitlines():
        if line.strip().lower().startswith(prefix.lower()):
            _head, _sep, tail = line.partition(":")
            return [part.strip() for part in tail.split(",") if part.strip()]
    return []


def _parsed_location_entry(entry: str) -> Optional[Tuple[str, int, int]]:
    match = _LOCATION_ENTRY_RE.search(entry.strip())
    if not match:
        return None
    try:
        start = int(match.group("start"))
        end = int(match.group("end") or start)
    except (TypeError, ValueError):
        return None
    return match.group("file"), start, end


def _answer_contract_file_tokens(answer: str) -> List[str]:
    tokens: List[str] = []
    tokens.extend(_contract_values(answer, "Files"))
    for symbol in _contract_values(answer, "Symbols"):
        if ":" not in symbol:
            continue
        file_part, _sym = symbol.split(":", 1)
        if "." in file_part:
            tokens.append(file_part)
    for location in _contract_values(answer, "Locations"):
        parsed = _parsed_location_entry(location)
        if parsed:
            tokens.append(parsed[0])
    return list(dict.fromkeys(token for token in tokens if token))


def _repo_relative_contract_token(
    raw: str,
    repo_path: Optional[str],
) -> Optional[str]:
    """Return ``raw`` as a repo-relative token when it already embeds the root."""

    from codeminer.eval.retrieval_eval import normalize_file_path

    token = normalize_file_path(raw)
    if not token or not repo_path:
        return token
    root_norm = normalize_file_path(os.path.abspath(repo_path))
    if root_norm and token.startswith(f"{root_norm.rstrip('/')}/"):
        return token[len(root_norm.rstrip("/")) + 1 :]
    if "/repo/" in token:
        suffix = token.rsplit("/repo/", 1)[1]
        if suffix:
            return normalize_file_path(suffix)
    return token


def _candidate_file_aliases(
    tokens: Sequence[str],
    candidate_paths: Sequence[str],
    *,
    blocked_tokens: Sequence[str] = (),
) -> Dict[str, str]:
    from codeminer.eval.retrieval_eval import normalize_file_path

    blocked = set(blocked_tokens or ())
    normalized_candidates = []
    for path in candidate_paths or []:
        normalized = normalize_file_path(path)
        if normalized:
            normalized_candidates.append(normalized)
    aliases: Dict[str, str] = {}
    for raw in tokens:
        token = normalize_file_path(raw)
        if not token or token in blocked or token in normalized_candidates:
            continue
        matches = {
            candidate
            for candidate in normalized_candidates
            if candidate.endswith(f"/{token}")
            or (
                "/" not in token
                and candidate.rsplit("/", 1)[-1] == token.rsplit("/", 1)[-1]
            )
        }
        if len(matches) == 1:
            aliases[token] = next(iter(matches))
    return aliases


def _repo_file_aliases(
    tokens: Sequence[str], repo_path: Optional[str], *, max_files: int = 20000
) -> Tuple[Dict[str, str], set[str]]:
    from codeminer.eval.retrieval_eval import normalize_file_path

    if not repo_path:
        return {}, set()
    root = Path(repo_path)
    if not root.exists():
        return {}, set()
    normalized_tokens = {
        normalize_file_path(token): _repo_relative_contract_token(token, repo_path)
        for token in tokens
    }
    normalized_tokens = {
        token: repo_rel
        for token, repo_rel in normalized_tokens.items()
        if token and repo_rel
    }
    if not normalized_tokens:
        return {}, set()
    matches: Dict[str, set[str]] = {token: set() for token in normalized_tokens}
    exact: set[str] = set()
    scanned = 0
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _ANSWER_PATH_ALIAS_SKIP_DIRS]
        for filename in files:
            scanned += 1
            if scanned > max_files:
                return {}, set(normalized_tokens)
            rel = normalize_file_path(
                os.path.relpath(os.path.join(base, filename), root)
            )
            if not rel:
                continue
            for token, repo_rel in normalized_tokens.items():
                if rel == repo_rel:
                    exact.add(token)
                    continue
                if rel.endswith(f"/{repo_rel}") or (
                    "/" not in repo_rel
                    and rel.rsplit("/", 1)[-1] == repo_rel.rsplit("/", 1)[-1]
                ):
                    matches[token].add(rel)
    aliases: Dict[str, str] = {}
    blocked: set[str] = set()
    for token, repo_rel in normalized_tokens.items():
        if token in exact:
            if repo_rel != token:
                aliases[token] = repo_rel
            else:
                blocked.add(token)
            continue
        paths = matches.get(token) or set()
        if len(paths) == 1:
            aliases[token] = next(iter(paths))
        elif len(paths) > 1:
            blocked.add(token)
    return aliases, blocked


def normalize_answer_path_aliases(
    answer: str,
    *,
    repo_path: Optional[str] = None,
    candidate_paths: Sequence[str] = (),
) -> Dict[str, Any]:
    """Normalize unique path aliases in final answer contract lines.

    This is a deterministic finalization repair, not a scoring relaxation: it
    rewrites only the agent's explicit ``Files:``, ``Symbols:``, and
    ``Locations:`` contract paths when a basename or suffix resolves to exactly
    one repo-relative path from read/retrieval evidence or the repository tree.
    """

    from codeminer.eval.retrieval_eval import normalize_file_path

    tokens = _answer_contract_file_tokens(answer)
    if not tokens:
        return {"answer": answer, "applied": False, "replacements": {}}
    aliases, blocked_tokens = _repo_file_aliases(tokens, repo_path)
    candidate_aliases = _candidate_file_aliases(
        tokens,
        candidate_paths,
        blocked_tokens=blocked_tokens,
    )
    aliases.update({k: v for k, v in candidate_aliases.items() if k not in aliases})
    unresolved = [
        token for token in tokens if normalize_file_path(token) not in aliases
    ]
    repo_aliases, _blocked = _repo_file_aliases(unresolved, repo_path)
    for token, alias in repo_aliases.items():
        aliases.setdefault(token, alias)
    if not aliases:
        return {"answer": answer, "applied": False, "replacements": {}}

    used_replacements: Dict[str, str] = {}

    def _replace_file(raw: str) -> str:
        normalized = normalize_file_path(raw)
        patched = aliases.get(normalized or "", raw)
        if normalized and patched != raw:
            used_replacements[normalized] = patched
        return patched

    changed = False
    out_lines: List[str] = []
    for line in (answer or "").splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        if lower.startswith("files:"):
            values = _contract_values(line, "Files")
            patched = [_replace_file(value) for value in values]
            changed = changed or patched != values
            out_lines.append(f"Files: {', '.join(patched)}")
            continue
        if lower.startswith("symbols:"):
            values = _contract_values(line, "Symbols")
            patched_symbols = []
            for value in values:
                if ":" not in value:
                    patched_symbols.append(value)
                    continue
                file_part, symbol_part = value.split(":", 1)
                patched_symbols.append(f"{_replace_file(file_part)}:{symbol_part}")
            changed = changed or patched_symbols != values
            out_lines.append(f"Symbols: {', '.join(patched_symbols)}")
            continue
        if lower.startswith("locations:"):
            values = _contract_values(line, "Locations")
            patched_locations = []
            for value in values:
                parsed = _parsed_location_entry(value)
                if parsed is None:
                    patched_locations.append(value)
                    continue
                file, start, end = parsed
                patched_file = _replace_file(file)
                suffix = f"{start}-{end}" if end != start else str(start)
                patched_locations.append(f"{patched_file}:{suffix}")
            changed = changed or patched_locations != values
            out_lines.append(f"Locations: {', '.join(patched_locations)}")
            continue
        out_lines.append(line)
    patched_answer = "\n".join(out_lines)
    return {
        "answer": patched_answer if changed else answer,
        "applied": changed,
        "replacements": used_replacements if changed else {},
    }


def normalize_answer_location_order(
    answer: str,
    *,
    trace_payload: Optional[Dict[str, Any]] = None,
    top_k: int = 5,
) -> Dict[str, Any]:
    """Promote evidence-backed Locations entries into the scored top-k.

    This pass never invents a new location and never uses ground truth. It only
    reorders the final ``Locations:`` entries when spans already present later
    in the answer overlap read-confirmed or scheduled graph/LSP evidence.
    """

    from codeminer.eval.retrieval_eval import normalize_file_path
    from scripts.agent_compile.lib.trace_summary import trace_evidence_spans

    if top_k <= 0:
        top_k = 5
    location_values = _contract_values(answer, "Locations")
    if len(location_values) <= top_k:
        return {
            "answer": answer,
            "applied": False,
            "promotion_count": 0,
            "reason": "short_answer",
        }

    parsed_locations: List[Dict[str, Any]] = []
    for index, value in enumerate(location_values):
        parsed = _parsed_location_entry(value)
        if parsed is None:
            parsed_locations.append(
                {"entry": value, "index": index, "file": None, "start": 0, "end": 0}
            )
            continue
        file, start, end = parsed
        parsed_locations.append(
            {
                "entry": value,
                "index": index,
                "file": normalize_file_path(file),
                "start": start,
                "end": end,
            }
        )

    evidence_spans = trace_evidence_spans(trace_payload or {})

    def _evidence_for(loc: Dict[str, Any]) -> List[Dict[str, Any]]:
        file = loc.get("file")
        if not file:
            return []
        out = []
        for evidence in evidence_spans:
            if normalize_file_path(evidence.get("file")) != file:
                continue
            try:
                ev_start = int(evidence.get("start"))
                ev_end = int(evidence.get("end") or ev_start)
            except (TypeError, ValueError):
                continue
            if _span_overlaps(
                int(loc.get("start") or 0),
                int(loc.get("end") or 0),
                ev_start,
                ev_end,
            ):
                out.append(dict(evidence))
        return out

    role_rank = {
        "endpoint": 0,
        "bridge": 1,
        "provider": 2,
        "type": 3,
        "support": 4,
    }

    def _loc_len(loc: Dict[str, Any]) -> int:
        return max(0, int(loc.get("end") or 0) - int(loc.get("start") or 0) + 1)

    scored: List[Tuple[Tuple[int, int, int, int, int], Dict[str, Any]]] = []
    for loc in parsed_locations:
        evidence = _evidence_for(loc)
        best_role = min(
            (
                role_rank.get(str(ev.get("role") or "support"), 4)
                for ev in evidence
                if ev.get("role") is not None
            ),
            default=4,
        )
        read_confirmed = any(ev.get("read_confirmed") for ev in evidence)
        scheduled = any(ev.get("source") == "scheduled_graph_lsp" for ev in evidence)
        has_evidence = bool(evidence)
        short_helper = 1 if has_evidence and 0 < _loc_len(loc) <= 12 else 0
        if best_role < 4:
            evidence_rank = 0
        elif read_confirmed:
            evidence_rank = 1
        elif scheduled:
            evidence_rank = 2
        elif has_evidence:
            evidence_rank = 3
        else:
            evidence_rank = 4
        scored.append(
            (
                (
                    evidence_rank,
                    best_role,
                    short_helper,
                    -len(evidence),
                    int(loc.get("index") or 0),
                ),
                loc,
            )
        )

    ordered = [loc for _score, loc in sorted(scored, key=lambda item: item[0])]
    old_top_entries = [loc["entry"] for loc in parsed_locations[:top_k]]
    new_top_entries = [loc["entry"] for loc in ordered[:top_k]]
    promotions = [
        loc
        for loc in ordered[:top_k]
        if int(loc.get("index") or 0) >= top_k
        and loc.get("entry") not in old_top_entries
    ]
    if not promotions or new_top_entries == old_top_entries:
        return {
            "answer": answer,
            "applied": False,
            "promotion_count": 0,
            "reason": "no_evidence_promotion",
        }

    changed = False
    out_lines: List[str] = []
    reordered_values = [str(loc["entry"]) for loc in ordered]
    for line in (answer or "").splitlines():
        if line.strip().lower().startswith("locations:"):
            out_lines.append(f"Locations: {', '.join(reordered_values)}")
            changed = True
        else:
            out_lines.append(line)
    return {
        "answer": "\n".join(out_lines) if changed else answer,
        "applied": changed,
        "promotion_count": len(promotions) if changed else 0,
        "reason": "evidence_promoted_into_top_k" if changed else "no_change",
        "promoted_locations": [loc["entry"] for loc in promotions],
    }


def salvage_answer_schema_from_trace(
    answer: str,
    *,
    trace_payload: Optional[Dict[str, Any]] = None,
    read_windows: Sequence[Dict[str, Any]] = (),
    top_k: int = 5,
) -> Dict[str, Any]:
    """Build missing Locations from explicit files and read-confirmed evidence.

    This is a constrained ``format_gap`` repair. It only fires when the answer
    lacks a ``Locations:`` line but already names files/symbols in the contract,
    and it uses only read-confirmed trace spans or read tool windows for those
    explicit files.
    """

    from codeminer.eval.retrieval_eval import normalize_file_path
    from scripts.agent_compile.lib.trace_summary import trace_evidence_spans

    if _contract_values(answer, "Locations"):
        return {
            "answer": answer,
            "applied": False,
            "location_count": 0,
            "reason": "has_locations",
        }
    if top_k <= 0:
        top_k = 5

    files: List[str] = []
    for file in _contract_values(answer, "Files"):
        normalized = normalize_file_path(file)
        if normalized:
            files.append(normalized)
    for symbol in _contract_values(answer, "Symbols"):
        if ":" not in symbol:
            continue
        file_part, _symbol_part = symbol.split(":", 1)
        normalized = normalize_file_path(file_part)
        if normalized and "." in normalized:
            files.append(normalized)
    files = list(dict.fromkeys(files))
    if not files:
        return {
            "answer": answer,
            "applied": False,
            "location_count": 0,
            "reason": "no_explicit_files",
        }

    evidence: List[Dict[str, Any]] = []
    for span in trace_evidence_spans(trace_payload or {}):
        if span.get("read_confirmed"):
            evidence.append(dict(span))
    for read in read_windows or []:
        file = normalize_file_path(read.get("file_path") or read.get("path"))
        if not file:
            continue
        try:
            start = int(read.get("offset") or read.get("start") or 1)
        except (TypeError, ValueError):
            start = 1
        try:
            limit = int(read.get("limit") or 0)
        except (TypeError, ValueError):
            limit = 0
        end = start + limit - 1 if limit > 0 else start
        evidence.append(
            {
                "source": "read",
                "state": "read",
                "file": file,
                "start": start,
                "end": end,
                "read_confirmed": True,
                "read_turn": read.get("turn"),
            }
        )

    locations: List[Tuple[int, int, str]] = []
    seen_ranges: set[Tuple[str, int, int]] = set()
    explicit = set(files)
    for rank, span in enumerate(evidence):
        file = normalize_file_path(span.get("file"))
        if file not in explicit:
            continue
        try:
            start = int(span.get("start"))
            end = int(span.get("end") or start)
        except (TypeError, ValueError):
            continue
        if start <= 0 or end <= 0:
            continue
        if end < start:
            start, end = end, start
        key = (file, start, end)
        if key in seen_ranges:
            continue
        seen_ranges.add(key)
        turn = span.get("read_turn")
        try:
            turn_rank = int(turn) if turn is not None else 10**6
        except (TypeError, ValueError):
            turn_rank = 10**6
        locations.append((turn_rank, rank, f"{file}:{start}-{end}"))
    locations = sorted(locations)[:top_k]
    if not locations:
        return {
            "answer": answer,
            "applied": False,
            "location_count": 0,
            "reason": "no_read_confirmed_evidence",
        }

    existing_symbols = _contract_values(answer, "Symbols")
    salvaged = "\n".join(
        [
            f"Files: {', '.join(files)}",
            f"Symbols: {', '.join(existing_symbols)}",
            "Locations: " + ", ".join(location for _turn, _rank, location in locations),
        ]
    )
    return {
        "answer": salvaged,
        "applied": True,
        "location_count": len(locations),
        "reason": "read_confirmed_schema_salvage",
    }


def scheduled_top_anchor_required_anchor_audit(
    answer: str,
    audit: Dict[str, Any],
    *,
    repo_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Check whether a correction answer put required anchors in top-k."""
    from codeminer.eval.retrieval_eval import normalize_file_path, parse_answer_spans

    top_k = max(1, int(audit.get("top_k") or 5))
    required = audit.get("missing_anchors") or []
    answer_spans = parse_answer_spans(answer or "", repo_path)
    top_spans = answer_spans[:top_k]
    missing: List[Dict[str, Any]] = []

    def _overlaps_answer(loc: Dict[str, Any]) -> bool:
        file = normalize_file_path(loc.get("file"))
        if not file:
            return False
        try:
            start = int(loc.get("start_line"))
            end = int(loc.get("end_line") or start)
        except (TypeError, ValueError):
            return False
        for span in top_spans:
            try:
                span_start = int(span.get("start") or span.get("start_line") or 0)
                span_end = int(span.get("end") or span.get("end_line") or span_start)
            except (TypeError, ValueError):
                continue
            if normalize_file_path(span.get("file")) == file and _span_overlaps(
                start, end, span_start, span_end
            ):
                return True
        return False

    for loc in required:
        if not _overlaps_answer(loc):
            missing.append(dict(loc))

    return {
        "ok": bool(required) and bool(answer_spans) and not missing,
        "top_k": top_k,
        "required_count": len(required),
        "answer_span_count": len(answer_spans),
        "missing_count": len(missing),
        "missing_anchors": missing,
    }


def apply_scheduled_top_anchor_answer_patch(
    answer: str, audit: Dict[str, Any], *, allow_insert: bool = False
) -> Optional[str]:
    """Deterministically promote read-confirmed top anchors into Locations.

    By default this only promotes anchors already present later in the answer.
    ``allow_insert`` is used by the harness after ``missing_unread_count == 0``:
    the required anchor is trace/read-confirmed route evidence, so inserting its
    exact range is a deterministic finalization repair rather than discovery.
    """
    if not audit.get("triggered"):
        return None
    if int(audit.get("missing_unread_count") or 0) != 0:
        return None
    missing = audit.get("missing_anchors") or []
    if not missing:
        return None

    top_k = max(1, int(audit.get("top_k") or 5))
    required_locations: List[Tuple[str, int, int, str]] = []
    required_files: List[str] = []
    required_symbols: List[str] = []
    for anchor in missing:
        file = str(anchor.get("file") or "").strip()
        if not file:
            continue
        try:
            start = int(anchor.get("start_line"))
            end = int(anchor.get("end_line") or start)
        except (TypeError, ValueError):
            continue
        if start <= 0 or end <= 0:
            continue
        location = f"{file}:{start}-{end}"
        required_locations.append((file, start, end, location))
        required_files.append(file)
        name = str(anchor.get("name") or "").strip()
        if name:
            required_symbols.append(name)

    if not required_locations:
        return None

    existing_locations = _contract_values(answer, "Locations")
    if not existing_locations:
        return None

    def _overlapped_required_indexes(entry: str) -> set[int]:
        parsed = _parsed_location_entry(entry)
        if parsed is None:
            return set()
        file, start, end = parsed
        indexes: set[int] = set()
        for idx, (req_file, req_start, req_end, _req_location) in enumerate(
            required_locations
        ):
            if file == req_file and _span_overlaps(start, end, req_start, req_end):
                indexes.add(idx)
        return indexes

    tail_required_indexes: set[int] = set()
    for entry in existing_locations[top_k:]:
        tail_required_indexes.update(_overlapped_required_indexes(entry))
    if len(tail_required_indexes) < len(required_locations) and not allow_insert:
        return None

    patched_locations: List[str] = []
    for _file, _start, _end, location in required_locations:
        if location not in patched_locations:
            patched_locations.append(location)
    for entry in existing_locations:
        if len(patched_locations) >= top_k:
            break
        if _overlapped_required_indexes(entry):
            continue
        if entry not in patched_locations:
            patched_locations.append(entry)

    existing_files = _contract_values(answer, "Files")
    patched_files = list(dict.fromkeys(required_files + existing_files))
    existing_symbols = _contract_values(answer, "Symbols")
    patched_symbols = list(dict.fromkeys(required_symbols + existing_symbols))

    return "\n".join(
        [
            f"Files: {', '.join(patched_files)}",
            f"Symbols: {', '.join(patched_symbols)}",
            f"Locations: {', '.join(patched_locations)}",
        ]
    )


def scheduled_named_anchor_location_audit(
    result: Any, *, repo_path: Optional[str] = None, top_k: int = 5
) -> Dict[str, Any]:
    """Find read-confirmed scheduled anchors named in Symbols but absent in top-k.

    The final scorer can resolve ``Symbols:`` entries into spans, but those spans
    are appended after explicit ``Locations:`` ranges. If a model names a core
    scheduled anchor in ``Symbols`` while omitting its exact range from the
    first Locations entries, the content is often right but the answer ranks as
    a top-k miss. This audit detects that narrow, GT-free case.
    """
    from codeminer.eval.retrieval_eval import normalize_file_path, parse_answer_spans

    if top_k <= 0:
        top_k = 5
    answer = getattr(result, "answer", "") or ""
    symbols = _contract_values(answer, "Symbols")
    if not symbols:
        return {
            "triggered": False,
            "reason": "no_symbols_contract",
            "answer_span_count": 0,
            "top_k": top_k,
            "missing_count": 0,
            "missing_anchors": [],
        }

    symbol_leafs = {_leaf_symbol(symbol).lower() for symbol in symbols}
    symbol_leafs = {leaf for leaf in symbol_leafs if leaf}
    if not symbol_leafs:
        return {
            "triggered": False,
            "reason": "no_symbol_leafs",
            "answer_span_count": 0,
            "top_k": top_k,
            "missing_count": 0,
            "missing_anchors": [],
        }

    answer_spans = parse_answer_spans(answer, repo_path)
    top_spans = answer_spans[:top_k]
    offered = _scheduled_definition_locations(result)
    final_guard = _scheduled_final_guard_locations(result)
    read_scope = _merge_guard_locations(
        offered,
        final_guard,
        max_locations=len(offered) + len(final_guard),
    )
    read_confirmed = _read_confirmed_scheduled_locations(result, read_scope)

    def _overlaps_answer(loc: Dict[str, Any], spans: Sequence[Dict[str, Any]]) -> bool:
        file = normalize_file_path(loc.get("file"))
        if not file:
            return False
        try:
            start = int(loc.get("start_line"))
            end = int(loc.get("end_line") or start)
        except (TypeError, ValueError):
            return False
        for span in spans:
            try:
                span_start = int(span.get("start") or span.get("start_line") or 0)
                span_end = int(span.get("end") or span.get("end_line") or span_start)
            except (TypeError, ValueError):
                continue
            if normalize_file_path(span.get("file")) == file and _span_overlaps(
                start, end, span_start, span_end
            ):
                return True
        return False

    candidates: List[Dict[str, Any]] = []
    for loc in read_confirmed:
        role = str(loc.get("role") or "support")
        if role not in {"endpoint", "bridge", "provider", "type"}:
            continue
        name_leaf = _leaf_symbol(loc.get("name")).lower()
        if not name_leaf or name_leaf not in symbol_leafs:
            continue
        if _overlaps_answer(loc, top_spans):
            continue
        candidates.append(dict(loc))

    seen: set[Tuple[str, int, int, str]] = set()
    deduped: List[Dict[str, Any]] = []
    for loc in candidates:
        key = _candidate_key(loc)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(loc)

    ranked = [
        loc
        for _rank, loc in sorted(
            enumerate(deduped),
            key=lambda item: _scheduled_location_priority_key(
                item[1], original_rank=item[0]
            ),
        )
    ]
    reason = "none"
    if ranked:
        if any(_overlaps_answer(loc, answer_spans[top_k:]) for loc in ranked):
            reason = "named_anchor_outside_top_k"
        else:
            reason = "named_anchor_missing_location"

    return {
        "triggered": bool(ranked),
        "reason": reason,
        "answer_span_count": len(answer_spans),
        "top_k": top_k,
        "missing_count": len(ranked),
        "missing_anchors": ranked[:top_k],
    }


def apply_scheduled_named_anchor_location_patch(
    answer: str, audit: Dict[str, Any]
) -> Optional[str]:
    """Insert read-confirmed anchors named in Symbols into the Locations top-k."""
    if not audit.get("triggered"):
        return None
    missing = audit.get("missing_anchors") or []
    if not missing:
        return None
    top_k = max(1, int(audit.get("top_k") or 5))

    required_locations: List[Tuple[str, int, int, str]] = []
    required_files: List[str] = []
    required_symbols: List[str] = []
    for anchor in missing:
        file = str(anchor.get("file") or "").strip()
        if not file:
            continue
        try:
            start = int(anchor.get("start_line"))
            end = int(anchor.get("end_line") or start)
        except (TypeError, ValueError):
            continue
        if start <= 0 or end <= 0:
            continue
        location = f"{file}:{start}-{end}"
        required_locations.append((file, start, end, location))
        required_files.append(file)
        name = str(anchor.get("name") or "").strip()
        if name:
            required_symbols.append(name)

    if not required_locations:
        return None
    existing_locations = _contract_values(answer, "Locations")
    if not existing_locations:
        return None

    def _overlapped_required_indexes(entry: str) -> set[int]:
        parsed = _parsed_location_entry(entry)
        if parsed is None:
            return set()
        file, start, end = parsed
        indexes: set[int] = set()
        for idx, (req_file, req_start, req_end, _req_location) in enumerate(
            required_locations
        ):
            if file == req_file and _span_overlaps(start, end, req_start, req_end):
                indexes.add(idx)
        return indexes

    patched_locations: List[str] = []
    for _file, _start, _end, location in required_locations:
        if len(patched_locations) >= top_k:
            break
        if location not in patched_locations:
            patched_locations.append(location)
    for entry in existing_locations:
        if len(patched_locations) >= top_k:
            break
        if _overlapped_required_indexes(entry):
            continue
        if entry not in patched_locations:
            patched_locations.append(entry)

    existing_files = _contract_values(answer, "Files")
    patched_files = list(dict.fromkeys(required_files + existing_files))
    existing_symbols = _contract_values(answer, "Symbols")
    patched_symbols = list(dict.fromkeys(required_symbols + existing_symbols))

    return "\n".join(
        [
            f"Files: {', '.join(patched_files)}",
            f"Symbols: {', '.join(patched_symbols)}",
            f"Locations: {', '.join(patched_locations)}",
        ]
    )


def filter_answer_locations_to_trace_evidence(
    answer: str,
    *,
    trace_payload: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Remove explicit Location entries that have no trace evidence overlap."""
    from codeminer.eval.retrieval_eval import normalize_file_path
    from scripts.agent_compile.lib.trace_summary import trace_evidence_spans

    locations = _contract_values(answer, "Locations")
    if not locations:
        return {
            "answer": answer,
            "applied": False,
            "removed_count": 0,
            "reason": "no_locations_contract",
        }

    evidence_spans = trace_evidence_spans(trace_payload)
    if not evidence_spans:
        return {
            "answer": answer,
            "applied": False,
            "removed_count": 0,
            "reason": "no_trace_evidence",
        }

    def _entry_evidenced(entry: str) -> bool:
        parsed = _parsed_location_entry(entry)
        if parsed is None:
            return True
        file, start, end = parsed
        normalized = normalize_file_path(file)
        for ev in evidence_spans:
            ev_file = normalize_file_path(ev.get("file"))
            if ev_file != normalized:
                continue
            try:
                ev_start = int(ev.get("start") or 0)
                ev_end = int(ev.get("end") or ev_start)
            except (TypeError, ValueError):
                continue
            if _span_overlaps(start, end, ev_start, ev_end):
                return True
        return False

    kept: List[str] = []
    removed: List[str] = []
    for entry in locations:
        if _entry_evidenced(entry):
            kept.append(entry)
        else:
            removed.append(entry)

    if not removed:
        return {
            "answer": answer,
            "applied": False,
            "removed_count": 0,
            "reason": "all_locations_evidenced",
        }
    if not kept:
        return {
            "answer": answer,
            "applied": False,
            "removed_count": len(removed),
            "reason": "would_remove_all_locations",
            "removed_locations": removed,
        }

    files = _contract_values(answer, "Files")
    symbols = _contract_values(answer, "Symbols")
    patched = "\n".join(
        [
            f"Files: {', '.join(files)}",
            f"Symbols: {', '.join(symbols)}",
            f"Locations: {', '.join(kept)}",
        ]
    )
    return {
        "answer": patched,
        "applied": True,
        "removed_count": len(removed),
        "removed_locations": removed,
        "reason": "unconfirmed_location_filter",
    }


def render_scheduled_ordering_correction(audit: Dict[str, Any]) -> str:
    """Prompt preamble for one bounded final-evidence ordering correction."""
    if not audit.get("triggered"):
        return ""
    top_k = int(audit.get("top_k") or 5)
    lines = [
        "Scheduled graph/LSP answer ordering audit: your answer appears to have "
        f"more than {top_k} exact ranges, and the first {top_k} parsed ranges are "
        "dominated by helper methods. Re-answer the original localization "
        "request with a compact evidence shortlist.",
        f"Put at most {top_k} comma-separated entries in the final Locations line, "
        "using plain Files:/Symbols:/Locations: contract lines with no markdown "
        "bullets. Order entries by "
        "route importance: caller/entrypoint, bridge/factory, provider/value "
        "definition, then any remaining helper. Do not include exact file:line "
        "ranges for helper methods in prose before the final Locations line.",
    ]
    candidates = audit.get("tail_candidates") or []
    if candidates:
        lines.append("Locations already mentioned late that may need promotion:")
        for c in candidates:
            nm = c.get("name") or ""
            lines.append(
                f"- {c['file']}:{c['start_line']}-{c['end_line']} {nm}".rstrip()
            )
    return "\n".join(lines)


def _scheduled_context_state_with_trace_fallback(
    state: Dict[str, Any], trace: Any
) -> Dict[str, Any]:
    """Fill top-level scheduled fields from the persisted trace when needed."""
    out = dict(state)
    events = getattr(trace, "events", None) or []
    for event in events:
        if getattr(event, "kind", None) != "scheduled_context":
            continue
        data = getattr(event, "data", None) or {}
        if data.get("source") != "scheduled_graph_lsp":
            continue
        out["attempted"] = True
        out["triggered"] = True
        if not out.get("reason") and data.get("reason"):
            out["reason"] = data.get("reason")
        if not out.get("operation") and data.get("operation"):
            out["operation"] = data.get("operation")
        if not out.get("nodes") and data.get("node_count") is not None:
            out["nodes"] = int(data.get("node_count") or 0)
        if not out.get("seed_count") and data.get("seed_count") is not None:
            out["seed_count"] = int(data.get("seed_count") or 0)
        if not out.get("symbol_count") and data.get("symbol_count") is not None:
            out["symbol_count"] = int(data.get("symbol_count") or 0)
        break
    return out


def _scheduled_auto_open_context(
    locations: Sequence[Dict[str, Any]],
    *,
    repo_path: Optional[str],
    max_anchors: int = 1,
    max_lines: int = 80,
    query_terms: Optional[set[str]] = None,
    route_traceback: bool = False,
    skip_read_ranges: Sequence[Tuple[str, int, int]] = (),
) -> Tuple[str, List[Dict[str, Any]]]:
    """Render bounded source for the highest-priority scheduled route anchors."""
    if not repo_path or max_anchors <= 0 or max_lines <= 0:
        return "", []
    from codeminer.eval.retrieval_eval import normalize_file_path

    ranked_locations = _rank_route_auto_open_locations(
        locations,
        query_terms=query_terms or set(),
        route_traceback=route_traceback,
    )
    opened: List[Dict[str, Any]] = []
    lines = [
        "Auto-opened priority route source. Treat these spans as already read; "
        "ground the final Locations line in them when they explain the request:",
    ]
    repo = Path(repo_path)
    for loc in ranked_locations:
        if len(opened) >= max_anchors:
            break
        role = str(loc.get("role") or "support")
        if role not in {"endpoint", "bridge", "provider", "type"}:
            continue
        file = str(loc.get("file") or "")
        name = str(loc.get("name") or "")
        if not file:
            continue
        try:
            start = int(loc.get("start_line") or 0)
            end = int(loc.get("end_line") or start)
        except (TypeError, ValueError):
            continue
        if start <= 0 or end < start:
            continue
        normalized = normalize_file_path(file)
        if normalized and any(
            rfile == normalized and _span_overlaps(start, end, rstart, rend)
            for rfile, rstart, rend in skip_read_ranges
        ):
            continue
        span_len = end - start + 1
        if span_len <= 1:
            continue
        limit = min(max_lines, span_len)
        path = Path(file)
        if not path.is_absolute():
            path = repo / path
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                selected = [
                    line.rstrip()
                    for lineno, line in enumerate(fh, start=1)
                    if start <= lineno < start + limit
                ]
        except OSError:
            continue
        if not selected:
            continue
        opened_loc = {
            "file": file,
            "start_line": start,
            "end_line": start + len(selected) - 1,
            "name": name,
            "role": role,
            "source": loc.get("source"),
            "via": loc.get("via"),
        }
        opened.append(opened_loc)
        suffix = ""
        if loc.get("via"):
            suffix = f" via {loc.get('via')}"
        lines.append("")
        lines.append(
            f"### {file}:{start}-{end} {name} [{role}; {loc.get('source')}]" f"{suffix}"
        )
        for lineno, content in enumerate(selected, start=start):
            lines.append(f"{lineno:6d} | {content}")
        if len(selected) < span_len:
            lines.append(f"... ({span_len - len(selected)} more lines in anchor)")
    if not opened:
        return "", []
    return "\n".join(lines), opened


def _run_state_read_ranges(run_state: Dict[str, Any]) -> List[Tuple[str, int, int]]:
    """Return normalized read ranges already requested by the live runner."""
    from codeminer.eval.retrieval_eval import normalize_file_path

    read_ranges: List[Tuple[str, int, int]] = []
    for tc in run_state.get("tool_calls") or []:
        if getattr(tc, "skill_id", None) != "read" or getattr(tc, "error", None):
            continue
        args = getattr(tc, "arguments", None) or {}
        file = normalize_file_path(args.get("file_path"))
        if not file:
            continue
        try:
            start = int(args.get("offset") or 1)
        except (TypeError, ValueError):
            start = 1
        try:
            limit = int(args.get("limit") or 0)
        except (TypeError, ValueError):
            limit = 0
        end = start + limit - 1 if limit > 0 else 10**12
        read_ranges.append((file, start, end))
    return read_ranges


def _scheduled_route_guard_locations(
    locations: Sequence[Dict[str, Any]],
    *,
    max_locations: int = 5,
    query_terms: Optional[set[str]] = None,
    route_traceback: bool = False,
) -> List[Dict[str, Any]]:
    type_locs = [
        dict(loc) for loc in locations or [] if str(loc.get("role") or "") == "type"
    ]
    core_locs = [
        dict(loc)
        for loc in locations or []
        if str(loc.get("role") or "") in {"endpoint", "bridge", "provider"}
    ]
    out: List[Dict[str, Any]] = []
    seen: set[Tuple[str, int, int, str]] = set()

    def _take(loc: Dict[str, Any]) -> None:
        if len(out) >= max_locations:
            return
        key = _candidate_key(loc)
        if key in seen:
            return
        seen.add(key)
        out.append(dict(loc))

    if len(type_locs) >= 2:
        for loc in core_locs[:1]:
            _take(loc)
        for loc in type_locs[:2]:
            _take(loc)
        return out

    if route_traceback:
        ranked = _rank_traceback_guard_locations(
            locations,
            query_terms=query_terms or set(),
        )
        for loc in ranked:
            _take(loc)
        return out

    ranked = _rank_route_auto_open_locations(
        locations,
        query_terms=query_terms or set(),
        route_traceback=False,
    )
    for loc in ranked:
        _take(loc)
    return out


def _scheduled_route_budget_locations(
    locations: Sequence[Dict[str, Any]],
    *,
    max_locations: int,
    query_terms: Optional[set[str]] = None,
    route_traceback: bool = False,
) -> List[Dict[str, Any]]:
    """Trim a static-LSP route to the anchors worth injecting into context."""
    if max_locations <= 0:
        return []
    if len(locations or []) <= max_locations:
        return [dict(loc) for loc in locations or []]
    ranked = _scheduled_route_guard_locations(
        locations,
        max_locations=len(locations or []),
        query_terms=query_terms,
        route_traceback=route_traceback,
    )
    out = ranked[:max_locations]
    if not route_traceback:
        return out

    via_leafs = {
        _leaf_symbol(loc.get("via")).lower()
        for loc in locations or []
        if loc.get("source") == "route_neighbor" and loc.get("via")
    }
    protected = [
        dict(loc)
        for loc in locations or []
        if str(loc.get("source") or "") == "direct_definition"
        and str(loc.get("role") or "") in {"bridge", "provider", "type"}
        and _leaf_symbol(loc.get("name")).lower() in via_leafs
    ]
    if not protected:
        return out

    out_keys = {_candidate_key(loc) for loc in out}
    protected_keys = {_candidate_key(loc) for loc in protected}

    def _victim_priority(loc: Dict[str, Any]) -> Tuple[int, int]:
        role = str(loc.get("role") or "support")
        source = str(loc.get("source") or "")
        if source == "route_neighbor":
            return (0, 0)
        if role == "support":
            return (5, 0)
        if source != "route_neighbor" and role == "provider":
            return (4, 0)
        if source != "route_neighbor" and role == "endpoint":
            return (3, 0)
        if source != "route_neighbor" and role == "bridge":
            return (2, 0)
        if source != "route_neighbor" and role == "type":
            return (1, 0)
        return (0, 0)

    for loc in protected:
        key = _candidate_key(loc)
        if key in out_keys:
            continue
        replace_idx: Optional[int] = None
        replace_rank: Optional[Tuple[int, int, int]] = None
        for idx, current in enumerate(out):
            current_key = _candidate_key(current)
            if current_key in protected_keys:
                continue
            priority = _victim_priority(current)
            if priority[0] <= 0:
                continue
            candidate_rank = (priority[0], idx, int(current.get("start_line") or 0))
            if replace_rank is None or candidate_rank > replace_rank:
                replace_rank = candidate_rank
                replace_idx = idx
        if replace_idx is None:
            continue
        out_keys.discard(_candidate_key(out[replace_idx]))
        out[replace_idx] = dict(loc)
        out_keys.add(key)
    return out


def _route_budget_must_keep_keys(
    locations: Sequence[Dict[str, Any]],
) -> set[Tuple[str, int, int, str]]:
    """Route-core anchors a dynamic budget is not allowed to remove."""
    via_leafs = {
        _leaf_symbol(loc.get("via")).lower()
        for loc in locations or []
        if loc.get("source") == "route_neighbor" and loc.get("via")
    }
    keep: set[Tuple[str, int, int, str]] = set()
    for loc in locations or []:
        role = str(loc.get("role") or "support")
        source = str(loc.get("source") or "")
        leaf = _leaf_symbol(loc.get("name")).lower()
        if (
            source == "route_neighbor"
            or role in {"bridge", "type"}
            or (
                source == "direct_definition"
                and role in {"bridge", "provider", "type"}
                and leaf in via_leafs
            )
        ):
            keep.add(_candidate_key(loc))
    return keep


def _route_budget_anchor_summary(loc: Dict[str, Any]) -> Dict[str, Any]:
    """Small trace payload for a route anchor a dynamic budget could not keep."""
    return {
        "file": loc.get("file"),
        "start_line": loc.get("start_line"),
        "end_line": loc.get("end_line"),
        "name": loc.get("name"),
        "role": loc.get("role"),
        "source": loc.get("source"),
        "via": loc.get("via"),
    }


def _is_route_budget_noise(loc: Dict[str, Any]) -> bool:
    role = str(loc.get("role") or "support")
    file = str(loc.get("file") or "").lower()
    if role == "support":
        return True
    return str(loc.get("source") or "") == "direct_definition" and (
        "/test" in file or "tests/" in file or file.startswith("docs/")
    )


def _scheduled_route_dynamic_budget_locations(
    locations: Sequence[Dict[str, Any]],
    *,
    target_locations: int,
    query_terms: Optional[set[str]] = None,
    route_traceback: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Apply a conservative dynamic route budget with an explainable decision."""
    raw = [dict(loc) for loc in locations or []]
    metadata = {
        "applied": False,
        "reason": "not_needed",
        "target": target_locations,
        "raw_count": len(raw),
        "dropped_count": 0,
    }
    if target_locations <= 0 or len(raw) <= target_locations:
        return raw, metadata

    must_keep = _route_budget_must_keep_keys(raw)
    budgeted = _scheduled_route_budget_locations(
        raw,
        max_locations=target_locations,
        query_terms=query_terms,
        route_traceback=route_traceback,
    )
    budgeted_keys = {_candidate_key(loc) for loc in budgeted}
    if must_keep <= budgeted_keys:
        metadata.update(
            {
                "applied": True,
                "reason": "target_budget",
                "dropped_count": len(raw) - len(budgeted),
            }
        )
        return budgeted, metadata

    support_trimmed = [loc for loc in raw if not _is_route_budget_noise(loc)]
    support_keys = {_candidate_key(loc) for loc in support_trimmed}
    if len(support_trimmed) < len(raw) and must_keep <= support_keys:
        metadata.update(
            {
                "applied": True,
                "reason": "support_noise_only",
                "target": len(support_trimmed),
                "dropped_count": len(raw) - len(support_trimmed),
            }
        )
        return support_trimmed, metadata

    metadata.update(
        {
            "reason": "unsafe_missing_core",
            "missing_required_count": len(must_keep - budgeted_keys),
            "missing_required": [
                _route_budget_anchor_summary(loc)
                for loc in raw
                if _candidate_key(loc) in must_keep - budgeted_keys
            ][:5],
        }
    )
    return raw, metadata


def _run_state_read_confirmed_route_locations(
    run_state: Dict[str, Any],
    locations: Sequence[Dict[str, Any]],
    *,
    auto_open_locations: Sequence[Dict[str, Any]] = (),
) -> List[Dict[str, Any]]:
    """Return scheduled route anchors already read in the live runner state."""
    from codeminer.eval.retrieval_eval import normalize_file_path

    read_ranges = _run_state_read_ranges(run_state)

    for loc in auto_open_locations or []:
        file = normalize_file_path(loc.get("file"))
        if not file:
            continue
        try:
            start = int(loc.get("start_line"))
            end = int(loc.get("end_line") or start)
        except (TypeError, ValueError):
            continue
        read_ranges.append((file, start, end))

    confirmed: List[Dict[str, Any]] = []
    for loc in locations or []:
        file = normalize_file_path(loc.get("file"))
        if not file:
            continue
        try:
            start = int(loc.get("start_line"))
            end = int(loc.get("end_line") or start)
        except (TypeError, ValueError):
            continue
        if any(
            rfile == file and _span_overlaps(start, end, rstart, rend)
            for rfile, rstart, rend in read_ranges
        ):
            confirmed.append(dict(loc))
    return confirmed


def _scheduled_route_read_memory_metadata(
    run_state: Dict[str, Any],
    locations: Sequence[Dict[str, Any]],
    *,
    auto_open_locations: Sequence[Dict[str, Any]] = (),
    max_locations: int = 8,
) -> Dict[str, Any]:
    """Trace-only read/route coverage summary for offline scheduler tuning."""
    confirmed = _run_state_read_confirmed_route_locations(
        run_state,
        locations,
        auto_open_locations=auto_open_locations,
    )
    confirmed_keys = {_candidate_key(loc) for loc in confirmed}
    core_roles = {"endpoint", "bridge", "provider", "type"}
    unread = [
        dict(loc)
        for loc in locations or []
        if _candidate_key(loc) not in confirmed_keys
    ]
    confirmed_core = [
        loc for loc in confirmed if str(loc.get("role") or "") in core_roles
    ]
    unread_core = [loc for loc in unread if str(loc.get("role") or "") in core_roles]
    protected_keys = _route_budget_must_keep_keys(locations)
    confirmed_protected = [
        loc for loc in confirmed if _candidate_key(loc) in protected_keys
    ]
    unread_protected = [loc for loc in unread if _candidate_key(loc) in protected_keys]
    return {
        "route_read_confirmed_count": len(confirmed),
        "route_read_confirmed_core_count": len(confirmed_core),
        "route_read_confirmed_protected_count": len(confirmed_protected),
        "route_unread_count": len(unread),
        "route_unread_core_count": len(unread_core),
        "route_unread_protected_count": len(unread_protected),
        "route_read_confirmed_locations": [
            _route_budget_anchor_summary(loc) for loc in confirmed[:max_locations]
        ],
        "route_unread_core_locations": [
            _route_budget_anchor_summary(loc) for loc in unread_core[:max_locations]
        ],
        "route_unread_protected_locations": [
            _route_budget_anchor_summary(loc)
            for loc in unread_protected[:max_locations]
        ],
    }


def _build_scheduled_final_guard_locations(
    run_state: Dict[str, Any],
    route_locations: Sequence[Dict[str, Any]],
    guard_locations: Sequence[Dict[str, Any]],
    *,
    auto_open_locations: Sequence[Dict[str, Any]] = (),
    route_traceback: bool = False,
    query_terms: Optional[set[str]] = None,
    max_locations: int = 5,
) -> List[Dict[str, Any]]:
    """Return finalization guard anchors using read-confirmed route evidence."""
    route_query_terms = query_terms or set()
    out = list(guard_locations or [])
    if route_locations:
        read_confirmed = _run_state_read_confirmed_route_locations(
            run_state,
            route_locations,
            auto_open_locations=auto_open_locations,
        )
        protected_route = [
            loc
            for loc in guard_locations or []
            if loc.get("source") == "route_neighbor"
            and str(loc.get("role") or "") in {"endpoint", "bridge", "provider"}
        ]
        if route_traceback:
            read_confirmed = _rank_traceback_guard_locations(
                read_confirmed,
                query_terms=route_query_terms,
            )
            merge_primary = (
                list(protected_route) + list(guard_locations) + read_confirmed
            )
        else:
            merge_primary = list(protected_route) + read_confirmed
        out = _merge_guard_locations(
            merge_primary,
            guard_locations,
            max_locations=max_locations,
        )
        if route_traceback:
            out = _rank_traceback_guard_locations(
                out,
                query_terms=route_query_terms,
            )[:max_locations]
    return [dict(loc) for loc in out[:max_locations]]


_TRACEBACK_GUARD_TERM_WEIGHTS = {
    "run": 5,
    "queue": 5,
    "iter": 5,
    "iteration": 5,
    "recover": 4,
    "recovery": 4,
    "reset": 4,
    "fallback": 3,
    "collect": 3,
    "collected": 3,
    "process": 3,
    "trigger": 3,
    "trigg": 3,
    "validat": 3,
    "validation": 3,
    "action": 1,
}


def _traceback_guard_term_score(terms: set[str], query_terms: set[str]) -> int:
    matched = terms & query_terms
    return sum(_TRACEBACK_GUARD_TERM_WEIGHTS.get(term, 1) for term in matched)


def _rank_traceback_guard_locations(
    locations: Sequence[Dict[str, Any]],
    *,
    query_terms: set[str],
) -> List[Dict[str, Any]]:
    """Rank traceback route anchors by causal-path signal before helper breadth."""

    role_rank = {
        "endpoint": 0,
        "bridge": 1,
        "provider": 2,
        "type": 3,
        "support": 4,
    }

    def _key(
        item: Tuple[int, Dict[str, Any]]
    ) -> Tuple[int, int, int, int, int, int, int]:
        original_rank, loc = item
        try:
            start = int(loc.get("start_line") or 0)
            end = int(loc.get("end_line") or start)
        except (TypeError, ValueError):
            start = 0
            end = 0
        span_len = max(0, end - start + 1)
        one_line_decl = 1 if span_len <= 1 else 0
        route_neighbor = loc.get("source") == "route_neighbor" and str(
            loc.get("role") or ""
        ) in {"endpoint", "bridge", "provider"}
        leaf_terms = _route_text_terms(_leaf_symbol(loc.get("name")))
        text_terms = _route_text_terms(_candidate_text(loc))
        leaf_score = _traceback_guard_term_score(leaf_terms, query_terms)
        text_score = _traceback_guard_term_score(text_terms, query_terms)
        leaf_overlap = len(leaf_terms & query_terms)
        return (
            one_line_decl,
            0 if route_neighbor else 1,
            role_rank.get(str(loc.get("role") or "support"), 4),
            -leaf_score,
            -leaf_overlap,
            -text_score,
            original_rank,
        )

    return [loc for _rank, loc in sorted(enumerate(locations or []), key=_key)]


def _route_final_guard_priority_count(
    locations: Sequence[Dict[str, Any]],
    *,
    route_traceback: bool,
    query_terms: set[str],
) -> int:
    if not locations:
        return 0
    if not route_traceback:
        return len(locations)
    priority_count = 1
    for idx, loc in enumerate(list(locations)[:2], start=1):
        leaf_terms = _route_text_terms(_leaf_symbol(loc.get("name")))
        via_terms = _route_text_terms(str(loc.get("via") or ""))
        score = max(
            _traceback_guard_term_score(leaf_terms, query_terms),
            _traceback_guard_term_score(via_terms, query_terms),
        )
        if score >= 3:
            priority_count = max(priority_count, idx)
    return min(priority_count, len(locations))


def _merge_guard_locations(
    primary: Sequence[Dict[str, Any]],
    fallback: Sequence[Dict[str, Any]],
    *,
    max_locations: int = 5,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: set[Tuple[str, int, int, str]] = set()
    for loc in list(primary or []) + list(fallback or []):
        if len(out) >= max_locations:
            break
        key = _candidate_key(loc)
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(loc))
    return out


def render_scheduled_route_finalization_guard(
    locations: Sequence[Dict[str, Any]],
    *,
    top_k: int = 5,
) -> str:
    if not locations:
        return ""
    lines = [
        "Scheduled graph/LSP route finalization guard: stop broad search now "
        "and answer the original localization request from the route anchors "
        "already offered/read. Do not call more tools unless one of these exact "
        "anchors is impossible to use.",
        f"Put at most {top_k} comma-separated entries in the final Locations "
        "line, using plain Files:/Symbols:/Locations: contract lines. Keep "
        "these route anchors before semantic helper or implementation-detail "
        "ranges:",
    ]
    for loc in locations[:top_k]:
        nm = loc.get("name") or ""
        role = loc.get("role") or "support"
        lines.append(
            f"- {loc['file']}:{loc['start_line']}-{loc['end_line']} "
            f"{nm} [{role}]".rstrip()
        )
    return "\n".join(lines)


def render_scheduled_weak_route_skip(
    *,
    symbols: Sequence[str],
    node_count: int,
) -> str:
    if not symbols:
        return ""
    symbol_text = ", ".join(str(symbol) for symbol in symbols[:5])
    return "\n".join(
        [
            "Scheduled graph/LSP route check: the read-derived symbols only "
            f"resolved weak provider/support anchors ({node_count} candidates), "
            "so no graph route anchors were injected.",
            "Do not broaden search because of this weak route result. If the "
            "spans you already read explain the localization request, finalize "
            "now from those exact read-confirmed Locations; otherwise do at "
            "most one targeted grep/read for the missing caller or guard.",
            f"Checked symbols: {symbol_text}",
        ]
    )


def make_graph_context_scheduler(
    nav: Any,
    preload_candidates: Sequence[Dict[str, Any]],
    preload_spec: Dict[str, Any],
    *,
    max_turns: int,
    query: Optional[str] = None,
    repo_path: Optional[str] = None,
) -> Tuple[Callable[[Dict[str, Any]], Optional[Dict[str, Any]]], Dict[str, Any]]:
    """Return a one-shot live-loop graph context scheduler and its state.

    Unlike the post-run ``graph_on_fanout`` retry, this runs inside
    ``AgentRunner`` after each tool turn. When the trace shows search fan-out or
    turn-budget pressure, it injects bounded 1-hop graph neighbours as a normal
    user message so the model can course-correct before its final answer.
    """
    from scripts.agent_compile.lib.verify_expand import (
        expansion_seeds_from_candidate_spans,
        expansion_seeds_from_candidates,
        render_fanout_expansion,
        render_lsp_definition_context,
        render_lsp_route_context,
    )

    scheduled_state: Dict[str, Any] = {
        "attempted": False,
        "triggered": False,
        "skipped": False,
        "skip_reason": None,
        "reason": None,
        "nodes": 0,
        "seed_count": 0,
        "symbol_count": 0,
        "verified_preload": 0,
        "operation": None,
        "route_fanout_hold_count": 0,
        "route_fanout_hold_first_turn": None,
        "route_fanout_hold_read_calls": None,
        "route_fanout_hold_search_calls": None,
        "route_fanout_hold_generic_min_reads": None,
        "route_fanout_hold_reason": None,
    }

    def _scheduler(run_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if scheduled_state["attempted"]:
            if (
                scheduled_state.get("triggered")
                and scheduled_state.get("operation") == "lsp_route"
                and (
                    scheduled_state.get("route_type_heavy")
                    or scheduled_state.get("route_traceback")
                    or int(scheduled_state.get("route_provider_query_count") or 0) >= 2
                    or (
                        scheduled_state.get("route_bridge_query")
                        and bool(
                            preload_spec.get(
                                "graph_read_symbol_bridge_final_guard", True
                            )
                        )
                    )
                )
                and not scheduled_state.get("final_guard_attempted")
                and bool(preload_spec.get("graph_read_symbol_final_guard", True))
            ):
                trigger_turn = int(scheduled_state.get("trigger_turn") or 0)
                delay_turns = int(
                    preload_spec.get("graph_read_symbol_final_guard_delay_turns", 1)
                )
                current_turn = int(run_state.get("turn") or 0)
                if current_turn >= trigger_turn + delay_turns:
                    guard_locations = list(scheduled_state.get("guard_locations") or [])
                    route_locations = list(scheduled_state.get("route_locations") or [])
                    route_query_terms = set(
                        scheduled_state.get("route_query_terms") or []
                    )
                    max_guard_locations = int(
                        preload_spec.get(
                            "graph_read_symbol_final_guard_max_locations", 5
                        )
                    )
                    guard_locations = _build_scheduled_final_guard_locations(
                        run_state,
                        route_locations,
                        guard_locations,
                        auto_open_locations=list(
                            scheduled_state.get("auto_open_locations") or []
                        ),
                        route_traceback=bool(scheduled_state.get("route_traceback")),
                        query_terms=route_query_terms,
                        max_locations=max_guard_locations,
                    )
                    guard = render_scheduled_route_finalization_guard(guard_locations)
                    scheduled_state["final_guard_attempted"] = True
                    if guard:
                        guard_priority_count = _route_final_guard_priority_count(
                            guard_locations,
                            route_traceback=bool(
                                scheduled_state.get("route_traceback")
                            ),
                            query_terms=set(
                                scheduled_state.get("route_query_terms") or []
                            ),
                        )
                        return {
                            "source": "scheduled_graph_lsp",
                            "content": guard,
                            "metadata": {
                                "reason": "route_finalization_guard",
                                "operation": "lsp_route_finalization_guard",
                                "node_count": len(guard_locations),
                                "priority_count": guard_priority_count,
                                "locations": guard_locations,
                                "trigger_turn": trigger_turn,
                                "bridge_route_query": bool(
                                    scheduled_state.get("route_bridge_query")
                                ),
                            },
                        }
            return None

        scheduler_query = str(
            query if query is not None else run_state.get("query") or ""
        )
        scheduler_query_terms = _query_terms(scheduler_query)
        traceback_route_query = _is_traceback_route_query(scheduler_query_terms)
        bridge_read_route_query = _is_bridge_read_route_query(scheduler_query_terms)
        read_calls = sum(
            1
            for tc in run_state.get("tool_calls") or []
            if getattr(tc, "skill_id", None) == "read"
        )

        def _fanout_pressure() -> Dict[str, Any]:
            return tool_call_pressure(
                run_state.get("tool_calls") or [],
                turns=int(run_state.get("turn") or 0),
                max_turns=max_turns,
                min_search_calls=int(preload_spec.get("graph_fanout_search_calls", 4)),
                min_read_calls=(
                    int(preload_spec["graph_fanout_read_calls"])
                    if preload_spec.get("graph_fanout_read_calls") is not None
                    else None
                ),
                turn_ratio=float(preload_spec.get("graph_fanout_turn_ratio", 0.75)),
            )

        def _record_route_fanout_hold(generic_min_reads: int) -> None:
            pressure = _fanout_pressure()
            if not pressure.get("triggered"):
                return
            scheduled_state["route_fanout_hold_count"] = (
                int(scheduled_state.get("route_fanout_hold_count") or 0) + 1
            )
            turn = int(run_state.get("turn") or 0)
            if scheduled_state.get("route_fanout_hold_first_turn") is None:
                scheduled_state["route_fanout_hold_first_turn"] = turn
                scheduled_state["route_fanout_hold_read_calls"] = read_calls
                scheduled_state["route_fanout_hold_search_calls"] = int(
                    pressure.get("search_calls") or 0
                )
                scheduled_state["route_fanout_hold_generic_min_reads"] = (
                    generic_min_reads
                )
                scheduled_state["route_fanout_hold_reason"] = pressure.get("reason")

        hold_fanout_for_route_query = False
        if preload_spec.get("graph_schedule_on_read_symbols"):
            min_reads = int(preload_spec.get("graph_read_symbol_min_reads", 2))
            can_route = bool(
                preload_spec.get("graph_read_symbol_lsp_route", True)
                and hasattr(nav, "route_for_symbols")
            )
            can_define = hasattr(nav, "definitions_for_symbols")
            default_generic_min_reads = max(
                min_reads,
                3 if traceback_route_query else 4,
            )
            if (
                bridge_read_route_query
                and "graph_bridge_read_symbol_min_reads" in preload_spec
            ):
                default_generic_min_reads = max(
                    min_reads,
                    int(preload_spec["graph_bridge_read_symbol_min_reads"]),
                )
            generic_min_reads = int(
                preload_spec.get(
                    "graph_generic_read_symbol_min_reads",
                    default_generic_min_reads,
                )
            )
            if (
                can_route
                and read_calls > 0
                and read_calls < generic_min_reads
                and (traceback_route_query or bridge_read_route_query)
                and bool(
                    preload_spec.get(
                        "graph_read_symbol_hold_fanout_for_route_queries", True
                    )
                )
            ):
                hold_fanout_for_route_query = True
            if read_calls >= min_reads and (can_route or can_define):
                scoped_symbols = read_outputs_have_scoped_symbols(
                    run_state.get("read_outputs") or []
                )
                if not scoped_symbols and read_calls < generic_min_reads:
                    if hold_fanout_for_route_query:
                        _record_route_fanout_hold(generic_min_reads)
                    return None
                max_read_symbols = int(
                    preload_spec.get("graph_read_symbol_max_symbols", 8)
                )
                symbols = read_symbol_hints(
                    run_state.get("read_outputs") or [],
                    max_symbols=max_read_symbols,
                    query=scheduler_query,
                )
                fallback_symbol_count = int(
                    preload_spec.get(
                        "graph_read_symbol_preload_max_symbols",
                        8 if traceback_route_query else 4,
                    )
                )
                fallback_symbols = preload_symbol_hints(
                    nav,
                    preload_candidates,
                    max_symbols=fallback_symbol_count,
                    query=scheduler_query,
                    line_slack=int(
                        preload_spec.get("graph_read_symbol_preload_line_slack", 12)
                    ),
                )
                symbols = _merge_symbol_hints(
                    symbols,
                    fallback_symbols,
                    max_symbols=int(
                        preload_spec.get(
                            "graph_read_symbol_total_max_symbols",
                            max_read_symbols + fallback_symbol_count,
                        )
                    ),
                )
                if symbols:
                    base_max_symbol_nodes = int(
                        preload_spec.get("graph_read_symbol_max_nodes", 5)
                    )
                    max_symbol_nodes = base_max_symbol_nodes
                    pool_nodes = int(
                        preload_spec.get(
                            "graph_read_symbol_pool_nodes",
                            max(max_symbol_nodes * 3, max_symbol_nodes + 5),
                        )
                    )
                    neighbor_nodes = int(
                        preload_spec.get("graph_read_symbol_neighbor_nodes", 12)
                    )
                    operation = "lsp_route"
                    if traceback_route_query and can_route:
                        max_symbol_nodes = max(
                            max_symbol_nodes,
                            int(
                                preload_spec.get(
                                    "graph_read_symbol_traceback_max_nodes", 12
                                )
                            ),
                        )
                        pool_nodes = max(
                            pool_nodes,
                            int(
                                preload_spec.get(
                                    "graph_read_symbol_traceback_pool_nodes", 30
                                )
                            ),
                        )
                        neighbor_nodes = max(
                            neighbor_nodes,
                            int(
                                preload_spec.get(
                                    "graph_read_symbol_traceback_neighbor_nodes", 80
                                )
                            ),
                        )
                    if can_route:
                        neighbours = nav.route_for_symbols(
                            symbols,
                            max_nodes=max_symbol_nodes,
                            query=scheduler_query,
                            pool_nodes=pool_nodes,
                            neighbor_nodes=neighbor_nodes,
                        )
                    elif preload_spec.get("graph_read_symbol_role_quota"):
                        operation = "lsp_definition_role_quota"
                        neighbours = scheduled_definition_candidates_for_symbols(
                            nav,
                            symbols,
                            max_nodes=max_symbol_nodes,
                            query=scheduler_query,
                            pool_nodes=pool_nodes,
                            neighbor_nodes=neighbor_nodes,
                        )
                    else:
                        operation = "lsp_definition"
                        neighbours = nav.definitions_for_symbols(
                            symbols,
                            max_nodes=max_symbol_nodes,
                        )
                    raw_neighbour_count = len(neighbours)
                    roles = [
                        str(c.get("role"))
                        for c in neighbours
                        if c.get("role") is not None
                    ]
                    if (
                        operation == "lsp_route"
                        and neighbours
                        and not bool(
                            preload_spec.get(
                                "graph_read_symbol_allow_weak_lsp_route", False
                            )
                        )
                        and not any(
                            role in {"endpoint", "bridge", "type"} for role in roles
                        )
                    ):
                        weak_route_nudge = ""
                        if bool(
                            preload_spec.get("graph_read_symbol_weak_route_nudge", True)
                        ):
                            weak_route_nudge = render_scheduled_weak_route_skip(
                                symbols=symbols,
                                node_count=len(neighbours),
                            )
                        scheduled_state.update(
                            {
                                "attempted": True,
                                "triggered": bool(weak_route_nudge),
                                "skipped": True,
                                "skip_reason": "weak_route_roles",
                                "reason": "read_symbols",
                                "nodes": len(neighbours),
                                "seed_count": 0,
                                "symbol_count": len(symbols),
                                "operation": operation,
                            }
                        )
                        if weak_route_nudge:
                            return {
                                "source": "scheduled_graph_lsp",
                                "content": weak_route_nudge,
                                "metadata": {
                                    "reason": "weak_route_roles",
                                    "operation": "lsp_route_weak_skip",
                                    "read_calls": read_calls,
                                    "min_read_calls": min_reads,
                                    "generic_min_read_calls": generic_min_reads,
                                    "bridge_route_query": bridge_read_route_query,
                                    "symbols": symbols,
                                    "fallback_symbols": fallback_symbols,
                                    "symbol_count": len(symbols),
                                    "node_count": len(neighbours),
                                    "query_chars": len(scheduler_query),
                                    "route_roles": roles,
                                },
                            }
                        return None
                    if operation == "lsp_route":
                        route_budget_meta = {
                            "applied": False,
                            "reason": "disabled",
                            "target": max_symbol_nodes,
                            "raw_count": raw_neighbour_count,
                            "dropped_count": 0,
                        }
                        explicit_budget = "graph_read_symbol_lsp_route_budget_nodes"
                        explicit_traceback_budget = (
                            "graph_read_symbol_traceback_route_budget_nodes"
                        )
                        if explicit_budget in preload_spec or (
                            traceback_route_query
                            and explicit_traceback_budget in preload_spec
                        ):
                            budget_nodes = int(
                                preload_spec.get(
                                    explicit_budget,
                                    max_symbol_nodes,
                                )
                            )
                            if traceback_route_query:
                                budget_nodes = int(
                                    preload_spec.get(
                                        explicit_traceback_budget,
                                        budget_nodes,
                                    )
                                )
                            neighbours = _scheduled_route_budget_locations(
                                neighbours,
                                max_locations=budget_nodes,
                                query_terms=scheduler_query_terms,
                                route_traceback=traceback_route_query,
                            )
                            route_budget_meta.update(
                                {
                                    "applied": raw_neighbour_count > len(neighbours),
                                    "reason": "explicit",
                                    "target": budget_nodes,
                                    "dropped_count": raw_neighbour_count
                                    - len(neighbours),
                                }
                            )
                        elif bool(
                            preload_spec.get(
                                "graph_read_symbol_dynamic_route_budget", False
                            )
                        ):
                            dynamic_target = int(
                                preload_spec.get(
                                    "graph_read_symbol_dynamic_route_budget_nodes",
                                    (
                                        base_max_symbol_nodes
                                        if traceback_route_query
                                        else max_symbol_nodes
                                    ),
                                )
                            )
                            (
                                neighbours,
                                route_budget_meta,
                            ) = _scheduled_route_dynamic_budget_locations(
                                neighbours,
                                target_locations=dynamic_target,
                                query_terms=scheduler_query_terms,
                                route_traceback=traceback_route_query,
                            )
                        route_auto_open_candidates = [dict(loc) for loc in neighbours]
                        if traceback_route_query and bool(
                            preload_spec.get(
                                "graph_read_symbol_traceback_route_prompt_order",
                                False,
                            )
                        ):
                            neighbours = _scheduled_route_prompt_locations(
                                neighbours,
                                query_terms=scheduler_query_terms,
                                route_traceback=True,
                            )
                        roles = [
                            str(c.get("role"))
                            for c in neighbours
                            if c.get("role") is not None
                        ]
                    else:
                        route_budget_meta = {
                            "applied": False,
                            "reason": "not_lsp_route",
                            "target": len(neighbours),
                            "raw_count": raw_neighbour_count,
                            "dropped_count": 0,
                        }
                    if operation == "lsp_route":
                        extra = render_lsp_route_context(
                            neighbours,
                            reason="read_symbols",
                            symbols=symbols,
                        )
                    else:
                        extra = render_lsp_definition_context(
                            neighbours,
                            reason="read_symbols",
                            symbols=symbols,
                        )
                    auto_open_extra = ""
                    auto_open_locations: List[Dict[str, Any]] = []
                    query_provider_count = 0
                    auto_open_enabled = bool(
                        preload_spec.get(
                            "graph_read_symbol_auto_open",
                            operation == "lsp_route",
                        )
                    )
                    if extra and operation == "lsp_route" and auto_open_enabled:
                        route_type_count = sum(
                            1 for c in neighbours if str(c.get("role") or "") == "type"
                        )
                        auto_open_candidates = route_auto_open_candidates
                        if traceback_route_query:
                            auto_open_candidates = _scheduled_route_guard_locations(
                                route_auto_open_candidates,
                                max_locations=len(neighbours),
                                query_terms=scheduler_query_terms,
                                route_traceback=True,
                            )
                        query_provider_count = _route_query_provider_count(
                            auto_open_candidates,
                            query_terms=scheduler_query_terms,
                        )
                        auto_open_default_anchors = (
                            2 if route_type_count >= 2 or traceback_route_query else 1
                        )
                        if query_provider_count >= 2:
                            auto_open_default_anchors = max(
                                auto_open_default_anchors,
                                min(3, query_provider_count),
                            )
                        (
                            auto_open_extra,
                            auto_open_locations,
                        ) = _scheduled_auto_open_context(
                            auto_open_candidates,
                            repo_path=repo_path,
                            max_anchors=int(
                                preload_spec.get(
                                    "graph_read_symbol_auto_open_anchors",
                                    auto_open_default_anchors,
                                )
                            ),
                            max_lines=int(
                                preload_spec.get(
                                    "graph_read_symbol_auto_open_max_lines", 80
                                )
                            ),
                            query_terms=scheduler_query_terms,
                            route_traceback=traceback_route_query,
                            skip_read_ranges=_run_state_read_ranges(run_state),
                        )
                        if auto_open_extra:
                            extra = f"{extra}\n\n{auto_open_extra}"
                    scheduled_state.update(
                        {
                            "attempted": bool(extra),
                            "triggered": bool(extra),
                            "reason": "read_symbols" if extra else None,
                            "nodes": len(neighbours),
                            "seed_count": 0,
                            "symbol_count": len(symbols),
                            "operation": operation if extra else None,
                        }
                    )
                    if extra:
                        route_type_heavy = (
                            sum(
                                1
                                for c in neighbours
                                if str(c.get("role") or "") == "type"
                            )
                            >= 2
                        )
                        guard_locations = _scheduled_route_guard_locations(
                            neighbours,
                            query_terms=scheduler_query_terms,
                            route_traceback=traceback_route_query,
                        )
                        route_read_memory = {}
                        if operation == "lsp_route":
                            route_read_memory = _scheduled_route_read_memory_metadata(
                                run_state,
                                neighbours,
                                auto_open_locations=auto_open_locations,
                            )
                        scheduled_state.update(
                            {
                                "route_type_heavy": route_type_heavy,
                                "route_traceback": (
                                    traceback_route_query and operation == "lsp_route"
                                ),
                                "route_bridge_query": (
                                    bridge_read_route_query and operation == "lsp_route"
                                ),
                                "route_provider_query_count": query_provider_count,
                                "route_query_terms": sorted(scheduler_query_terms),
                                "guard_locations": guard_locations,
                                "route_locations": [dict(loc) for loc in neighbours],
                                "auto_open_locations": [
                                    dict(loc) for loc in auto_open_locations
                                ],
                                **route_read_memory,
                                "trigger_turn": int(run_state.get("turn") or 0),
                                "final_guard_attempted": False,
                            }
                        )
                        return {
                            "source": "scheduled_graph_lsp",
                            "content": extra,
                            "metadata": {
                                "reason": "read_symbols",
                                "operation": operation,
                                "read_calls": read_calls,
                                "min_read_calls": min_reads,
                                "generic_min_read_calls": generic_min_reads,
                                "bridge_route_query": bridge_read_route_query,
                                "symbols": symbols,
                                "fallback_symbols": fallback_symbols,
                                "symbol_count": len(symbols),
                                "node_count": len(neighbours),
                                "raw_node_count": raw_neighbour_count,
                                "route_budget_applied": bool(
                                    route_budget_meta.get("applied")
                                ),
                                "route_budget_reason": route_budget_meta.get("reason"),
                                "route_budget_target": route_budget_meta.get("target"),
                                "route_budget_dropped": route_budget_meta.get(
                                    "dropped_count"
                                ),
                                "route_budget_missing_required_count": (
                                    route_budget_meta.get("missing_required_count")
                                ),
                                "route_budget_missing_required": (
                                    route_budget_meta.get("missing_required")
                                ),
                                "query_chars": len(scheduler_query),
                                "locations": neighbours,
                                "auto_open_locations": auto_open_locations,
                                "auto_open_count": len(auto_open_locations),
                                **route_read_memory,
                                "route_roles": roles,
                                "role_quota": bool(
                                    preload_spec.get("graph_read_symbol_role_quota")
                                ),
                                "traceback_route": traceback_route_query,
                            },
                        }

        if hold_fanout_for_route_query:
            _record_route_fanout_hold(generic_min_reads)
            return None

        pressure = _fanout_pressure()
        if not pressure["triggered"]:
            return None

        verified_preload = _verified_preload_count(run_state.get("context") or [])
        scheduled_state["verified_preload"] = verified_preload
        skip_verified = int(preload_spec.get("graph_fanout_skip_verified_preload", 0))
        if skip_verified > 0 and verified_preload >= skip_verified:
            scheduled_state.update(
                {
                    "skipped": True,
                    "skip_reason": "preload_verified",
                    "reason": pressure["reason"],
                }
            )
            return None

        scheduled_state["attempted"] = True
        seeds = expansion_seeds_from_candidate_spans(
            nav,
            list(preload_candidates or []),
            max_seeds=int(preload_spec.get("graph_fanout_max_seeds", 5)),
        ) or expansion_seeds_from_candidates(list(preload_candidates or []))
        neighbours = nav.neighbors(
            seeds, max_nodes=int(preload_spec.get("graph_fanout_max_nodes", 10))
        )
        extra = render_fanout_expansion(
            neighbours, reason=f"scheduled_{pressure['reason']}"
        )
        scheduled_state.update(
            {
                "reason": pressure["reason"],
                "nodes": len(neighbours),
                "seed_count": len(seeds),
                "operation": "graph_fanout",
            }
        )
        if not extra:
            return None

        scheduled_state["triggered"] = True
        return {
            "source": "scheduled_graph_lsp",
            "content": extra,
            "metadata": {
                **pressure,
                "seed_count": len(seeds),
                "node_count": len(neighbours),
            },
        }

    return _scheduler, scheduled_state


def run_cell(
    cfg: SweepConfig,
    *,
    contexts: Dict[str, Any],
    llm: Any,
    repo_path: str,
    language_key: str,
    query: str,
    subset_id: str,
    skills: Sequence[str],
    preload_spec: Optional[Dict[str, Any]] = None,
    verify: bool = False,
    nav: Any = None,
) -> Dict[str, Any]:
    """Run one (subset) agent cell against already-loaded contexts.

    When ``preload_spec`` is given, retrieval candidates are computed up front
    and injected into the agent's opening prompt (the pre-load architecture);
    the returned ``preload_candidates`` lists the injected ``(file, span)``s for
    contribution attribution.

    When ``verify`` and a ``nav`` (GraphNav) are given, the committed answer is
    checked against the graph (deterministic); if it does not resolve to a real
    symbol, the 1-hop neighbours of the best seeds are injected and the agent
    answers once more. Token/turn costs of BOTH runs are summed so the verify
    arm is charged for the closed loop.
    """
    from codeminer.agent.runner import AgentRunner
    from codeminer.agent.skills.registry import SkillRegistry
    from codeminer.compiler.params import SessionContext
    from scripts.agent_compile.lib.orchestrator import (
        query_is_specific,
        scatter_gather_localize,
    )
    from scripts.agent_compile.lib.preload import assemble_preload

    runs_usage: List[Dict[str, Any]] = []
    runs_turns: List[int] = []
    preload_candidates: List[Dict[str, Any]] = []
    effective_query = query
    scatter_mode = False
    mode = (preload_spec or {}).get("mode")
    if preload_spec:
        # Adaptive gate: a VAGUE query (no concrete handle) gets NO candidates —
        # fall back to blank-slate grep, which beats pre-load on behavioral.
        # A SPECIFIC query gets the eager pre-load (cheap, accurate). One short
        # LLM call; usage is charged to the cell via runs_usage below.
        gated_skip = False
        if mode == "eager_gated":

            def _gate_call(msgs):
                # Disable chain-of-thought ONLY for Qwen3.5 (else the one-word
                # verdict is truncated mid-"Thinking Process"). The chat_template
                # kwarg is Qwen-specific — passing it to vertex/Anthropic errors,
                # so gate it on the model name.
                extra = {}
                if "qwen3" in (cfg.model or "").lower():
                    extra["extra_body"] = {
                        "chat_template_kwargs": {"enable_thinking": False}
                    }
                resp = llm._call_raw(msgs, **extra)
                u = getattr(resp, "usage", None)
                if u is not None:
                    runs_usage.append(
                        {
                            "prompt_tokens": getattr(u, "prompt_tokens", 0),
                            "completion_tokens": getattr(u, "completion_tokens", 0),
                            "total_tokens": getattr(u, "total_tokens", 0),
                        }
                    )
                return resp.choices[0].message.content

            gated_skip = not query_is_specific(_gate_call, query)

        if gated_skip:
            preload_candidates = []  # vague -> blank-slate grep
        else:
            preamble, preload_candidates = assemble_preload(
                contexts, query, recipe=preload_spec
            )
            # mode=scatter routes candidates to isolated verify-subagents instead
            # of injecting them into one context (see lib/orchestrator.py).
            if mode == "scatter":
                scatter_mode = True
            elif preamble:
                effective_query = f"{query}\n\n{preamble}"

    sctx = SessionContext(
        repo_path=repo_path, repo_size=1000, primary_language=language_key
    )
    # Seed-richness router for eager_compact: weak models can re-read from a
    # minimal collapse seed (the 4B "re-read tax"), so inline more read content
    # for them; strong models usually answer from the latest read plus
    # assessment. Recipe key
    # ``compact_keep_reads`` overrides the model-size heuristic.
    _keep_reads = (preload_spec or {}).get("compact_keep_reads")
    _tail_chars = (preload_spec or {}).get("compact_tail_chars")
    if _keep_reads is None:
        import re as _re

        # Dial measured on 4B (n=99): keep0 minimal seed HURTS weak models
        # (files@5 0.667 < grep 0.737); keep1 recovers to grep level (0.741) at
        # -18% cost; keep2 over-stuffs (worse than keep1). Mid/strong (>=9B,
        # Haiku) are fine with keep0 (already >= grep). So: weak -> 1, else 0.
        _mt = _re.search(r"(\d+(?:\.\d+)?)\s*b\b", (cfg.model or "").lower())
        _sz = float(_mt.group(1)) if _mt else 999.0
        _keep_reads = 1 if _sz <= 7 else 0

    def _empty_scheduled_state() -> Dict[str, Any]:
        return {
            "attempted": False,
            "triggered": False,
            "reason": None,
            "operation": None,
            "nodes": 0,
            "seed_count": 0,
            "symbol_count": 0,
            "skipped": False,
            "skip_reason": None,
            "verified_preload": 0,
        }

    def _new_scheduler():
        if (
            preload_spec
            and preload_spec.get("graph_schedule_on_fanout")
            and nav is not None
            and not scatter_mode
        ):
            return make_graph_context_scheduler(
                nav,
                preload_candidates,
                preload_spec,
                max_turns=cfg.max_turns,
                query=query,
                repo_path=repo_path,
            )
        return None, _empty_scheduled_state()

    def _new_runner(context_scheduler):
        return AgentRunner(
            llm=llm,
            registry=SkillRegistry(),
            max_turns=cfg.max_turns,
            allow_skills=set(skills),
            session_ctx=sctx,
            include_default_tools=cfg.include_default_tools,
            default_tool_ids=(
                set(cfg.default_tool_ids) if cfg.default_tool_ids is not None else None
            ),
            system_prompt=cfg.system_prompt,
            first_turn_tool_choice=getattr(cfg, "first_turn_tool_choice", None) or None,
            compact_after_read=(mode == "eager_compact"),
            compact_keep_reads=(int(_keep_reads) if mode == "eager_compact" else 0),
            compact_tail_chars=(
                int(_tail_chars)
                if mode == "eager_compact" and _tail_chars is not None
                else None
            ),
            context_scheduler=context_scheduler,
        )

    context_scheduler, scheduled_context_state = _new_scheduler()
    runner = _new_runner(context_scheduler)
    # The always-on default tools (file_read / file_search) resolve relative
    # paths against the process cwd, so run the agent from the instance repo.
    # The sweep is sequential, so chdir is safe here.
    prev_cwd = os.getcwd()

    def _do_run(
        q: str,
        *,
        trace_preload: Optional[List[Dict[str, Any]]] = None,
        reset_scheduler: bool = False,
        max_turns_override: Optional[int] = None,
    ):
        nonlocal context_scheduler, scheduled_context_state, runner
        if reset_scheduler:
            context_scheduler, scheduled_context_state = _new_scheduler()
            runner = _new_runner(context_scheduler)
        try:
            os.chdir(repo_path)
            r = runner.run(
                q,
                preload_candidates=trace_preload,
                max_turns=max_turns_override,
            )
        finally:
            os.chdir(prev_cwd)
        u = r.usage.to_dict() if r.usage else {}
        runs_usage.append(u.get("token_usage") or u or {})
        if r.total_turns is not None:
            runs_turns.append(r.total_turns)
        return r

    def _do_answer_only_run(q: str):
        # Empty allow_skills widens to the full registry by contract, so use a
        # non-existent sentinel while also disabling default tools.
        answer_only = AgentRunner(
            llm=llm,
            registry=SkillRegistry(),
            max_turns=int(
                (preload_spec or {}).get(
                    "graph_schedule_answer_only_correction_turns", 1
                )
            ),
            allow_skills={"__answer_only__"},
            session_ctx=sctx,
            include_default_tools=False,
            default_tool_ids=None,
            system_prompt=cfg.system_prompt,
            first_turn_tool_choice=None,
            compact_after_read=False,
            compact_keep_reads=0,
            context_scheduler=None,
        )
        try:
            os.chdir(repo_path)
            r = answer_only.run(q)
        finally:
            os.chdir(prev_cwd)
        u = r.usage.to_dict() if r.usage else {}
        runs_usage.append(u.get("token_usage") or u or {})
        if r.total_turns is not None:
            runs_turns.append(r.total_turns)
        return r

    def _do_read_only_correction_run(q: str):
        read_only = AgentRunner(
            llm=llm,
            registry=SkillRegistry(),
            max_turns=max(
                1,
                int(
                    (preload_spec or {}).get(
                        "graph_schedule_read_only_top_anchor_correction_turns",
                        3,
                    )
                ),
            ),
            allow_skills={"__read_only_correction__"},
            session_ctx=sctx,
            include_default_tools=True,
            default_tool_ids={"read"},
            system_prompt=cfg.system_prompt,
            first_turn_tool_choice=None,
            compact_after_read=(mode == "eager_compact"),
            compact_keep_reads=(int(_keep_reads) if mode == "eager_compact" else 0),
            compact_tail_chars=(
                int(_tail_chars)
                if mode == "eager_compact" and _tail_chars is not None
                else None
            ),
            context_scheduler=None,
        )
        try:
            os.chdir(repo_path)
            r = read_only.run(q, preload_candidates=preload_candidates)
        finally:
            os.chdir(prev_cwd)
        u = r.usage.to_dict() if r.usage else {}
        runs_usage.append(u.get("token_usage") or u or {})
        if r.total_turns is not None:
            runs_turns.append(r.total_turns)
        return r

    if scatter_mode:

        def _run_subagent(sys_prompt, q, mt):
            sub = AgentRunner(
                llm=llm,
                registry=SkillRegistry(),
                max_turns=mt,
                allow_skills=set(skills),
                session_ctx=sctx,
                include_default_tools=cfg.include_default_tools,
                default_tool_ids=(
                    set(cfg.default_tool_ids)
                    if cfg.default_tool_ids is not None
                    else None
                ),
                system_prompt=sys_prompt or cfg.system_prompt,
                first_turn_tool_choice=getattr(cfg, "first_turn_tool_choice", None)
                or None,
            )
            try:
                os.chdir(repo_path)
                r = sub.run(q)
            finally:
                os.chdir(prev_cwd)
            u = r.usage.to_dict() if r.usage else {}
            runs_usage.append(u.get("token_usage") or u or {})
            if r.total_turns is not None:
                runs_turns.append(r.total_turns)
            return r

        result = scatter_gather_localize(
            query, preload_candidates, _run_subagent, explore_turns=cfg.max_turns
        )
    else:
        result = _do_run(effective_query, trace_preload=preload_candidates)
    primary_scheduled_context_state = dict(scheduled_context_state)

    scheduled_anchor_audit_triggered = False
    scheduled_anchor_audit_offered = 0
    scheduled_anchor_audit_read = 0
    scheduled_anchor_audit_cited = 0
    scheduled_top_anchor_audit_triggered = False
    scheduled_top_anchor_audit_reason: Optional[str] = None
    scheduled_top_anchor_audit_missing = 0
    scheduled_top_anchor_audit_missing_unread = 0
    scheduled_ordering_audit_triggered = False
    scheduled_ordering_audit_reason: Optional[str] = None
    scheduled_ordering_audit_span_count = 0
    if (
        preload_spec
        and preload_spec.get("graph_schedule_audit_answer")
        and scheduled_context_state.get("triggered")
        and not scatter_mode
    ):
        from codeminer.agent.agent_types import AgentTraceEvent

        audit = scheduled_anchor_audit(result, repo_path=repo_path)
        scheduled_anchor_audit_offered = int(audit.get("offered_count") or 0)
        scheduled_anchor_audit_read = int(audit.get("read_count") or 0)
        scheduled_anchor_audit_cited = int(audit.get("cited_count") or 0)
        correction = render_scheduled_anchor_correction(audit.get("read_uncited") or [])
        if audit.get("triggered") and correction:
            scheduled_anchor_audit_triggered = True
            result = _do_run(
                f"{effective_query}\n\n{correction}",
                trace_preload=preload_candidates,
                reset_scheduler=True,
            )
            if result.trace is not None:
                result.trace.events.append(
                    AgentTraceEvent(
                        kind="scheduled_anchor_audit",
                        turn=0,
                        data={
                            "strategy": "read_symbol_definition_correction",
                            "offered_count": scheduled_anchor_audit_offered,
                            "read_count": scheduled_anchor_audit_read,
                            "cited_count": scheduled_anchor_audit_cited,
                            "correction_anchors": len(audit.get("read_uncited") or []),
                        },
                    )
                )
        top_anchor_priority_count = _scheduled_top_anchor_priority_count(
            _scheduled_definition_locations(result),
            configured=(
                int(preload_spec.get("graph_schedule_top_anchor_priority_count"))
                if preload_spec.get("graph_schedule_top_anchor_priority_count")
                is not None
                else None
            ),
        )
        top_anchor_audit = scheduled_top_anchor_audit(
            result,
            repo_path=repo_path,
            priority_count=top_anchor_priority_count,
        )
        scheduled_top_anchor_audit_reason = str(
            top_anchor_audit.get("reason") or "none"
        )
        scheduled_top_anchor_audit_missing = int(
            top_anchor_audit.get("missing_count") or 0
        )
        scheduled_top_anchor_audit_missing_unread = int(
            top_anchor_audit.get("missing_unread_count") or 0
        )
        top_anchor_correction = render_scheduled_top_anchor_correction(top_anchor_audit)
        if (
            not scheduled_anchor_audit_triggered
            and top_anchor_audit.get("triggered")
            and top_anchor_correction
        ):
            scheduled_top_anchor_audit_triggered = True
            correction_mode = "tool_loop"
            attempted_correction_mode: Optional[str] = None
            cascade_fallback = False
            correction_validation: Optional[Dict[str, Any]] = None

            def _validate_top_anchor_correction(
                candidate_result: Any, attempted_mode: str
            ) -> Any:
                nonlocal correction_mode, cascade_fallback, correction_validation
                correction_validation = scheduled_top_anchor_required_anchor_audit(
                    getattr(candidate_result, "answer", "") or "",
                    top_anchor_audit,
                    repo_path=repo_path,
                )
                if not bool(
                    preload_spec.get(
                        "graph_schedule_validate_top_anchor_correction", True
                    )
                ):
                    return candidate_result
                if correction_validation.get("ok"):
                    return candidate_result
                cascade_fallback = True
                correction_mode = f"{attempted_mode}_fallback"
                return _do_run(
                    f"{effective_query}\n\n{top_anchor_correction}",
                    trace_preload=preload_candidates,
                    reset_scheduler=True,
                )

            patched_answer = (
                apply_scheduled_top_anchor_answer_patch(
                    result.answer or "",
                    top_anchor_audit,
                    allow_insert=bool(
                        preload_spec.get(
                            "graph_schedule_insert_missing_top_anchor_correction",
                            True,
                        )
                    ),
                )
                if scheduled_top_anchor_audit_missing_unread == 0
                and bool(
                    preload_spec.get(
                        "graph_schedule_deterministic_top_anchor_correction", True
                    )
                )
                else None
            )
            if patched_answer:
                correction_mode = "deterministic_answer_patch"
                result.answer = patched_answer
                correction_validation = scheduled_top_anchor_required_anchor_audit(
                    result.answer or "", top_anchor_audit, repo_path=repo_path
                )
                if not correction_validation.get("ok"):
                    cascade_fallback = True
                    correction_mode = "deterministic_answer_patch_fallback"
                    result = _do_run(
                        f"{effective_query}\n\n{top_anchor_correction}",
                        trace_preload=preload_candidates,
                        reset_scheduler=True,
                    )
            elif scheduled_top_anchor_audit_missing_unread == 0 and bool(
                preload_spec.get(
                    "graph_schedule_read_only_top_anchor_correction",
                    False,
                )
            ):
                attempted_correction_mode = "read_only_tool_loop"
                correction_mode = "read_only_tool_loop"
                result = _validate_top_anchor_correction(
                    _do_read_only_correction_run(
                        f"{effective_query}\n\n{top_anchor_correction}"
                    ),
                    attempted_correction_mode,
                )
            elif scheduled_top_anchor_audit_missing_unread == 0 and bool(
                preload_spec.get(
                    "graph_schedule_answer_only_correction",
                    False,
                )
            ):
                attempted_correction_mode = "answer_only"
                correction_mode = "answer_only"
                correction_result = _do_answer_only_run(
                    f"{effective_query}\n\n{top_anchor_correction}"
                )
                result.answer = correction_result.answer
                result.messages = correction_result.messages
                result.total_turns = (result.total_turns or 0) + (
                    correction_result.total_turns or 0
                )
                result.usage = correction_result.usage
                result.usage_records = list(result.usage_records or []) + list(
                    correction_result.usage_records or []
                )
                result = _validate_top_anchor_correction(
                    result, attempted_correction_mode
                )
            elif scheduled_top_anchor_audit_missing_unread == 0 and bool(
                preload_spec.get("graph_schedule_bounded_top_anchor_correction", False)
            ):
                attempted_correction_mode = "bounded_tool_loop"
                correction_mode = "bounded_tool_loop"
                result = _validate_top_anchor_correction(
                    _do_run(
                        f"{effective_query}\n\n{top_anchor_correction}",
                        trace_preload=preload_candidates,
                        reset_scheduler=True,
                        max_turns_override=max(
                            1,
                            int(
                                preload_spec.get(
                                    "graph_schedule_bounded_top_anchor_correction_turns",
                                    2,
                                )
                            ),
                        ),
                    ),
                    attempted_correction_mode,
                )
            else:
                result = _do_run(
                    f"{effective_query}\n\n{top_anchor_correction}",
                    trace_preload=preload_candidates,
                    reset_scheduler=True,
                )
            if result.trace is not None:
                result.trace.events.append(
                    AgentTraceEvent(
                        kind="scheduled_top_anchor_audit",
                        turn=0,
                        data={
                            "strategy": "priority_anchor_top_k_correction",
                            "correction_mode": correction_mode,
                            "attempted_correction_mode": (
                                attempted_correction_mode or correction_mode
                            ),
                            "cascade_fallback": cascade_fallback,
                            "validation_ok": (
                                correction_validation.get("ok")
                                if correction_validation is not None
                                else None
                            ),
                            "validation_missing_count": (
                                correction_validation.get("missing_count")
                                if correction_validation is not None
                                else None
                            ),
                            "reason": scheduled_top_anchor_audit_reason,
                            "missing_count": scheduled_top_anchor_audit_missing,
                            "missing_unread_count": (
                                scheduled_top_anchor_audit_missing_unread
                            ),
                            "read_count": int(top_anchor_audit.get("read_count") or 0),
                            "answer_span_count": int(
                                top_anchor_audit.get("answer_span_count") or 0
                            ),
                            "top_k": int(top_anchor_audit.get("top_k") or 5),
                        },
                    )
                )
        named_anchor_audit = scheduled_named_anchor_location_audit(
            result, repo_path=repo_path
        )
        named_anchor_patch = apply_scheduled_named_anchor_location_patch(
            result.answer or "", named_anchor_audit
        )
        if named_anchor_patch:
            scheduled_ordering_audit_triggered = True
            scheduled_ordering_audit_reason = str(
                named_anchor_audit.get("reason") or "none"
            )
            scheduled_ordering_audit_span_count = int(
                named_anchor_audit.get("answer_span_count") or 0
            )
            result.answer = named_anchor_patch
            if result.trace is not None:
                result.trace.events.append(
                    AgentTraceEvent(
                        kind="scheduled_answer_ordering_audit",
                        turn=0,
                        data={
                            "strategy": "named_anchor_location_patch",
                            "reason": scheduled_ordering_audit_reason,
                            "answer_span_count": scheduled_ordering_audit_span_count,
                            "top_k": int(named_anchor_audit.get("top_k") or 5),
                            "tail_candidates": int(
                                named_anchor_audit.get("missing_count") or 0
                            ),
                        },
                    )
                )
        ordering_audit = (
            {"triggered": False, "reason": "named_anchor_patch_applied"}
            if named_anchor_patch
            else scheduled_answer_ordering_audit(result, repo_path=repo_path)
        )
        if not named_anchor_patch:
            scheduled_ordering_audit_reason = str(
                ordering_audit.get("reason") or "none"
            )
            scheduled_ordering_audit_span_count = int(
                ordering_audit.get("answer_span_count") or 0
            )
        ordering_correction = render_scheduled_ordering_correction(ordering_audit)
        if (
            not named_anchor_patch
            and ordering_audit.get("triggered")
            and ordering_correction
        ):
            scheduled_ordering_audit_triggered = True
            result = _do_run(
                f"{effective_query}\n\n{ordering_correction}",
                trace_preload=preload_candidates,
                reset_scheduler=True,
            )
            if result.trace is not None:
                result.trace.events.append(
                    AgentTraceEvent(
                        kind="scheduled_answer_ordering_audit",
                        turn=0,
                        data={
                            "strategy": "final_evidence_ordering_correction",
                            "reason": scheduled_ordering_audit_reason,
                            "answer_span_count": scheduled_ordering_audit_span_count,
                            "top_k": int(ordering_audit.get("top_k") or 5),
                            "dominant_file": ordering_audit.get("dominant_file"),
                            "dominant_file_count": ordering_audit.get(
                                "dominant_file_count"
                            ),
                            "short_top_count": ordering_audit.get("short_top_count"),
                            "tail_candidates": len(
                                ordering_audit.get("tail_candidates") or []
                            ),
                        },
                    )
                )

    if (
        primary_scheduled_context_state.get("attempted")
        or primary_scheduled_context_state.get("triggered")
        or primary_scheduled_context_state.get("skipped")
    ):
        current_scheduled_context_state = dict(scheduled_context_state)
        scheduled_context_state = dict(primary_scheduled_context_state)
        if current_scheduled_context_state.get("triggered"):
            scheduled_context_state["triggered"] = True
        if current_scheduled_context_state.get("attempted"):
            scheduled_context_state["attempted"] = True
        if current_scheduled_context_state.get("skipped"):
            scheduled_context_state["skipped"] = True
            scheduled_context_state["skip_reason"] = (
                current_scheduled_context_state.get("skip_reason")
                or scheduled_context_state.get("skip_reason")
            )
        scheduled_context_state["route_fanout_hold_count"] = int(
            primary_scheduled_context_state.get("route_fanout_hold_count") or 0
        ) + int(current_scheduled_context_state.get("route_fanout_hold_count") or 0)
        first_turns = [
            int(turn)
            for turn in (
                primary_scheduled_context_state.get("route_fanout_hold_first_turn"),
                current_scheduled_context_state.get("route_fanout_hold_first_turn"),
            )
            if turn is not None
        ]
        if first_turns:
            scheduled_context_state["route_fanout_hold_first_turn"] = min(first_turns)
        for key in (
            "route_fanout_hold_read_calls",
            "route_fanout_hold_search_calls",
            "route_fanout_hold_generic_min_reads",
            "route_fanout_hold_reason",
        ):
            scheduled_context_state[key] = (
                primary_scheduled_context_state.get(key)
                if primary_scheduled_context_state.get(key) is not None
                else current_scheduled_context_state.get(key)
            )

    graph_expansion_triggered = False
    graph_expansion_reason: Optional[str] = None
    graph_expansion_nodes = 0
    graph_expansion_resolved: Optional[int] = None
    if (
        preload_spec
        and preload_spec.get("graph_on_fanout")
        and nav is not None
        and not scatter_mode
    ):
        from codeminer.agent.agent_types import AgentTraceEvent
        from scripts.agent_compile.lib.verify_expand import (
            expansion_seeds_from_candidate_spans,
            expansion_seeds_from_candidates,
            graph_verify,
            render_fanout_expansion,
        )

        pressure = search_fanout_pressure(
            result,
            max_turns=cfg.max_turns,
            min_search_calls=int(preload_spec.get("graph_fanout_search_calls", 4)),
            min_read_calls=(
                int(preload_spec["graph_fanout_read_calls"])
                if preload_spec.get("graph_fanout_read_calls") is not None
                else None
            ),
            turn_ratio=float(preload_spec.get("graph_fanout_turn_ratio", 0.75)),
        )
        if pressure["triggered"]:
            verdict = graph_verify(result.answer or "", nav)
            graph_expansion_resolved = verdict.n_resolved
            seeds = (
                verdict.seeds
                or expansion_seeds_from_candidate_spans(
                    nav,
                    preload_candidates,
                    max_seeds=int(preload_spec.get("graph_fanout_max_seeds", 5)),
                )
                or expansion_seeds_from_candidates(preload_candidates)
            )
            neighbours = nav.neighbors(
                seeds, max_nodes=int(preload_spec.get("graph_fanout_max_nodes", 10))
            )
            extra = render_fanout_expansion(neighbours, reason=str(pressure["reason"]))
            if extra:
                graph_expansion_triggered = True
                graph_expansion_reason = str(pressure["reason"])
                graph_expansion_nodes = len(neighbours)
                result = _do_run(
                    f"{effective_query}\n\n{extra}",
                    trace_preload=preload_candidates,
                )
                if result.trace is not None:
                    result.trace.events.append(
                        AgentTraceEvent(
                            kind="graph_expansion",
                            turn=0,
                            data={
                                **pressure,
                                "seed_count": len(seeds),
                                "node_count": len(neighbours),
                            },
                        )
                    )

    # Verify-expand (Layer 4): if the committed answer is not anchored to a real
    # graph symbol, inject 1-hop neighbours of the best seeds and answer once more.
    verify_triggered = False
    verify_resolved: Optional[int] = None
    if verify and nav is not None:
        from scripts.agent_compile.lib.verify_expand import (
            expansion_seeds_from_candidates,
            graph_verify,
            render_expansion,
        )

        verdict = graph_verify(result.answer or "", nav)
        verify_resolved = verdict.n_resolved
        if not verdict.ok:
            seeds = verdict.seeds or expansion_seeds_from_candidates(preload_candidates)
            extra = render_expansion(nav.neighbors(seeds, max_nodes=10))
            if extra:
                verify_triggered = True
                result = _do_run(
                    f"{effective_query}\n\n{extra}",
                    trace_preload=preload_candidates,
                )

    nodes: List[Any] = []
    tool_calls: List[Dict[str, Any]] = []
    file_read_paths: List[str] = []
    file_reads: List[Dict[str, Any]] = []
    tool_call_turns = _tool_call_turns_by_id(result)
    for tc in result.tool_calls:
        n = len(tc.result) if isinstance(tc.result, list) else 0
        tool_calls.append(
            {
                "skill_id": tc.skill_id,
                "n_results": n,
                "error": bool(tc.error),
                "envelope": (
                    tc.envelope.to_dict()
                    if getattr(tc, "envelope", None) is not None
                    else None
                ),
            }
        )
        if isinstance(tc.result, list):
            nodes.extend(tc.result)
        if tc.skill_id == "read" and not tc.error:
            args = tc.arguments or {}
            p = args.get("file_path")
            if p:
                file_read_paths.append(str(p))
                # Capture the read window (1-based offset/limit) for audit (which
                # line range the agent inspected); not scored.
                read_record = {
                    "file_path": str(p),
                    "offset": args.get("offset"),
                    "limit": args.get("limit"),
                    "tool_call_id": tc.tool_call_id,
                }
                turn = tool_call_turns.get(tc.tool_call_id)
                if turn is not None:
                    read_record["turn"] = turn
                file_reads.append(read_record)

    def _node_file(node: Any) -> Optional[str]:
        node_id = getattr(node, "node_id", None)
        if node_id:
            return str(node_id).split(":", 1)[0]
        file = getattr(node, "file", None)
        return str(file) if file else None

    answer_path_alias_repair = normalize_answer_path_aliases(
        result.answer or "",
        repo_path=repo_path,
        candidate_paths=list(file_read_paths)
        + [str(c.get("file")) for c in preload_candidates if c.get("file")]
        + [path for path in (_node_file(node) for node in nodes) if path],
    )
    if answer_path_alias_repair.get("applied"):
        result.answer = str(
            answer_path_alias_repair.get("answer") or result.answer or ""
        )
        if result.trace is not None:
            from codeminer.agent.agent_types import AgentTraceEvent

            result.trace.events.append(
                AgentTraceEvent(
                    kind="answer_path_alias_normalization",
                    turn=0,
                    data={
                        "replacement_count": len(
                            answer_path_alias_repair.get("replacements") or {}
                        ),
                        "replacements": answer_path_alias_repair.get("replacements")
                        or {},
                    },
                )
            )

    answer_schema_salvage = salvage_answer_schema_from_trace(
        result.answer or "",
        trace_payload=result.trace.to_dict() if result.trace else None,
        read_windows=file_reads,
        top_k=5,
    )
    if answer_schema_salvage.get("applied"):
        result.answer = str(answer_schema_salvage.get("answer") or result.answer or "")
        if result.trace is not None:
            from codeminer.agent.agent_types import AgentTraceEvent

            result.trace.events.append(
                AgentTraceEvent(
                    kind="answer_schema_salvage",
                    turn=0,
                    data={
                        "location_count": int(
                            answer_schema_salvage.get("location_count") or 0
                        ),
                        "reason": answer_schema_salvage.get("reason"),
                    },
                )
            )

    answer_location_order_repair = normalize_answer_location_order(
        result.answer or "",
        trace_payload=result.trace.to_dict() if result.trace else None,
        top_k=5,
    )
    if answer_location_order_repair.get("applied"):
        result.answer = str(
            answer_location_order_repair.get("answer") or result.answer or ""
        )
        if result.trace is not None:
            from codeminer.agent.agent_types import AgentTraceEvent

            result.trace.events.append(
                AgentTraceEvent(
                    kind="answer_location_order_normalization",
                    turn=0,
                    data={
                        "promotion_count": int(
                            answer_location_order_repair.get("promotion_count") or 0
                        ),
                        "reason": answer_location_order_repair.get("reason"),
                        "promoted_locations": answer_location_order_repair.get(
                            "promoted_locations"
                        )
                        or [],
                    },
                )
            )

    answer_trace_evidence_filter = {
        "applied": False,
        "removed_count": 0,
        "reason": "disabled",
    }
    if preload_spec and bool(
        preload_spec.get("graph_schedule_filter_unconfirmed_locations", False)
    ):
        answer_trace_evidence_filter = filter_answer_locations_to_trace_evidence(
            result.answer or "",
            trace_payload=result.trace.to_dict() if result.trace else None,
        )
        if answer_trace_evidence_filter.get("applied"):
            result.answer = str(
                answer_trace_evidence_filter.get("answer") or result.answer or ""
            )
            if result.trace is not None:
                from codeminer.agent.agent_types import AgentTraceEvent

                result.trace.events.append(
                    AgentTraceEvent(
                        kind="answer_trace_evidence_filter",
                        turn=0,
                        data={
                            "removed_count": int(
                                answer_trace_evidence_filter.get("removed_count") or 0
                            ),
                            "reason": answer_trace_evidence_filter.get("reason"),
                            "removed_locations": answer_trace_evidence_filter.get(
                                "removed_locations"
                            )
                            or [],
                        },
                    )
                )

    # Sum token usage across the (1 or 2) runs so the verify arm is charged for
    # the closed loop, not just its final turn.
    def _sum(key: str) -> Optional[float]:
        vals = [u.get(key) for u in runs_usage if u.get(key) is not None]
        return sum(vals) if vals else None

    scheduled_context_state = _scheduled_context_state_with_trace_fallback(
        scheduled_context_state,
        result.trace,
    )

    return {
        "nodes": nodes,
        "tool_calls": tool_calls,
        "file_read_paths": file_read_paths,
        "file_reads": file_reads,
        "preload_candidates": preload_candidates,
        "answer": result.answer or "",
        "trace": result.trace.to_dict() if result.trace else None,
        "total_turns": sum(runs_turns) if runs_turns else result.total_turns,
        "total_duration_ms": result.total_duration_ms,
        "tool_call_count": len(result.tool_calls),
        "verify_triggered": verify_triggered,
        "verify_resolved": verify_resolved,
        "graph_expansion_triggered": graph_expansion_triggered,
        "graph_expansion_reason": graph_expansion_reason,
        "graph_expansion_nodes": graph_expansion_nodes,
        "graph_expansion_resolved": graph_expansion_resolved,
        "scheduled_context_attempted": scheduled_context_state["attempted"],
        "scheduled_context_triggered": scheduled_context_state["triggered"],
        "scheduled_context_reason": scheduled_context_state["reason"],
        "scheduled_context_operation": scheduled_context_state["operation"],
        "scheduled_context_nodes": scheduled_context_state["nodes"],
        "scheduled_context_seed_count": scheduled_context_state["seed_count"],
        "scheduled_context_skipped": scheduled_context_state["skipped"],
        "scheduled_context_skip_reason": scheduled_context_state["skip_reason"],
        "scheduled_context_verified_preload": scheduled_context_state[
            "verified_preload"
        ],
        "scheduled_route_fanout_hold_count": int(
            scheduled_context_state.get("route_fanout_hold_count") or 0
        ),
        "scheduled_route_fanout_hold_first_turn": scheduled_context_state.get(
            "route_fanout_hold_first_turn"
        ),
        "scheduled_route_fanout_hold_read_calls": scheduled_context_state.get(
            "route_fanout_hold_read_calls"
        ),
        "scheduled_route_fanout_hold_search_calls": scheduled_context_state.get(
            "route_fanout_hold_search_calls"
        ),
        "scheduled_route_fanout_hold_generic_min_reads": scheduled_context_state.get(
            "route_fanout_hold_generic_min_reads"
        ),
        "scheduled_route_fanout_hold_reason": scheduled_context_state.get(
            "route_fanout_hold_reason"
        ),
        "scheduled_anchor_audit_triggered": scheduled_anchor_audit_triggered,
        "scheduled_anchor_audit_offered": scheduled_anchor_audit_offered,
        "scheduled_anchor_audit_read": scheduled_anchor_audit_read,
        "scheduled_anchor_audit_cited": scheduled_anchor_audit_cited,
        "scheduled_top_anchor_audit_triggered": scheduled_top_anchor_audit_triggered,
        "scheduled_top_anchor_audit_reason": scheduled_top_anchor_audit_reason,
        "scheduled_top_anchor_audit_missing": scheduled_top_anchor_audit_missing,
        "scheduled_top_anchor_audit_missing_unread": (
            scheduled_top_anchor_audit_missing_unread
        ),
        "scheduled_ordering_audit_triggered": scheduled_ordering_audit_triggered,
        "scheduled_ordering_audit_reason": scheduled_ordering_audit_reason,
        "scheduled_ordering_audit_span_count": scheduled_ordering_audit_span_count,
        "answer_path_alias_normalized": bool(answer_path_alias_repair.get("applied")),
        "answer_path_alias_replacements": len(
            answer_path_alias_repair.get("replacements") or {}
        ),
        "answer_schema_salvaged": bool(answer_schema_salvage.get("applied")),
        "answer_schema_salvage_locations": int(
            answer_schema_salvage.get("location_count") or 0
        ),
        "answer_location_order_normalized": bool(
            answer_location_order_repair.get("applied")
        ),
        "answer_location_order_promotions": int(
            answer_location_order_repair.get("promotion_count") or 0
        ),
        "answer_trace_evidence_filtered": bool(
            answer_trace_evidence_filter.get("applied")
        ),
        "answer_trace_evidence_filter_removed": int(
            answer_trace_evidence_filter.get("removed_count") or 0
        ),
        "prompt_tokens": _sum("prompt_tokens"),
        "completion_tokens": _sum("completion_tokens"),
        "total_tokens": _sum("total_tokens"),
        "cost_usd": _sum("cost_usd"),
        "cache_read_input_tokens": _sum("cache_read_input_tokens"),
        "cache_creation_input_tokens": _sum("cache_creation_input_tokens"),
    }
