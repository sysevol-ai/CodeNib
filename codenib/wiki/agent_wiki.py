# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Agent wiki pipeline: a DeepWiki-style, high-level conceptual wiki.

Stage 1 (:mod:`outline`) proposes a conceptual page tree. This module is stage
2: for each page it retrieves the relevant code (by the page's keywords, via
the repo's embedding/BM25 index) and asks an LLM to write a grounded page —
prose plus an inline Mermaid ``graph TD`` diagram — and attaches the retrieved
symbols as (GitHub-linkable) citations. Outline + pages are disk-cached so a
repo's wiki is generated once.

Drop-in for the demo's ``WikiBuilder``: exposes ``page_tree`` / ``page`` /
``source`` so ``codenib/web/app.py`` can serve it unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, List, Optional

from ..log_utils import get_logger
from .builder import WikiBuilder
from .narrator import _no_thinking_kwargs
from .outline import generate_outline

logger = get_logger(__name__)

_EXT_LANG = {
    "py": "python",
    "go": "go",
    "rs": "rust",
    "ts": "typescript",
    "tsx": "typescript",
    "js": "javascript",
    "jsx": "javascript",
    "c": "c",
    "h": "cpp",
    "cpp": "cpp",
    "cs": "csharp",
    "java": "java",
    "rb": "ruby",
    "php": "php",
    "kt": "kotlin",
    "kts": "kotlin",
}
_MAX_CONTEXT_CHARS = 9000


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "page"


def _lang(file: str) -> str:
    return _EXT_LANG.get((file or "").rsplit(".", 1)[-1].lower(), "")


_PAGE_PROMPT = """\
You are writing one page of a developer wiki for the {repo} codebase.

Page title: {title}
What it should cover: {summary}

Below are the most relevant code symbols retrieved for this page. Ground your
explanation ONLY in these — do not invent files, symbols, or behavior.

{context}

Write the page as GitHub-flavored Markdown:
- Start with a short intro paragraph (no H1 title — the app adds it).
- Explain how this subsystem works at a high level, then the key pieces, in
  clear prose with `inline code` for symbol names. Use ## / ### subheadings.
- Be concrete and reference the real file/symbol names shown above.
- Do NOT draw any diagram — a dependency diagram is added automatically.
Return only the Markdown (no JSON, no commentary)."""


class AgentWiki:
    """Generate + serve a conceptual wiki for one repo (outline + pages)."""

    def __init__(
        self, bundle: Any, model: str, cache_dir: Optional[str] = None
    ) -> None:
        self._bundle = bundle
        self._model = model
        self._cache_dir = cache_dir
        self._wb = WikiBuilder(bundle)  # reuse source() + symbol/citation helpers
        self._outline: Optional[Dict[str, Any]] = None
        self._pages: Dict[str, Dict[str, Any]] = {}

    # -- caching -----------------------------------------------------------

    def _key(self, suffix: str) -> str:
        entry = self._bundle.entry
        commit = getattr(entry, "commit_short", "") or getattr(entry, "base_commit", "")
        raw = f"{getattr(entry, 'instance_id', 'repo')}@{commit}/{suffix}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    def _cache_path(self, suffix: str) -> Optional[str]:
        if not self._cache_dir:
            return None
        os.makedirs(self._cache_dir, exist_ok=True)
        return os.path.join(self._cache_dir, f"agentwiki_{self._key(suffix)}.json")

    def _read_cache(self, suffix: str) -> Optional[Any]:
        path = self._cache_path(suffix)
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh).get("data")
            except (OSError, json.JSONDecodeError):
                return None
        return None

    def _write_cache(self, suffix: str, data: Any) -> None:
        path = self._cache_path(suffix)
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"model": self._model, "data": data}, fh)
        except OSError:
            pass

    # -- outline + nav -----------------------------------------------------

    def outline(self) -> Dict[str, Any]:
        if self._outline is not None:
            return self._outline
        cached = self._read_cache("outline")
        if cached and cached.get("pages"):
            self._outline = cached
            return cached
        data = generate_outline(self._bundle, self._model)
        self._normalize(data.get("pages", []), seen=set(), first=True)
        self._outline = data
        if data.get("pages"):
            self._write_cache("outline", data)
        return data

    def _normalize(self, pages: List[Dict[str, Any]], seen: set, first: bool) -> None:
        """Ensure unique slug ids; force the first page id to 'overview'."""
        for i, p in enumerate(pages):
            pid = (
                "overview"
                if (first and i == 0)
                else _slug(p.get("id") or p.get("title"))
            )
            while pid in seen:
                pid += "-x"
            seen.add(pid)
            p["id"] = pid
            self._normalize(p.get("children", []) or [], seen, first=False)

    def page_tree(self) -> List[dict]:
        def refs(pages: List[Dict[str, Any]]) -> List[dict]:
            return [
                {
                    "id": p["id"],
                    "title": p.get("title", p["id"]),
                    "children": refs(p.get("children", []) or []),
                }
                for p in pages
            ]

        return refs(self.outline().get("pages", []))

    def _find(
        self, page_id: str, pages: Optional[List[Dict[str, Any]]] = None
    ) -> Optional[Dict[str, Any]]:
        if pages is None:
            pages = self.outline().get("pages", [])
        for p in pages:
            if p.get("id") == page_id:
                return p
            hit = self._find(page_id, p.get("children", []) or [])
            if hit:
                return hit
        return None

    # -- page generation ---------------------------------------------------

    def page(self, page_id: str) -> Optional[dict]:
        if page_id in self._pages:
            return self._pages[page_id]
        cached = self._read_cache(f"page_{page_id}")
        if cached:
            self._pages[page_id] = cached
            return cached
        meta = self._find(page_id)
        if meta is None:
            return None
        page = self._generate_page(meta)
        self._pages[page_id] = page
        self._write_cache(f"page_{page_id}", page)
        return page

    def _retrieve(self, meta: Dict[str, Any], top_k: int = 8) -> List[Any]:
        ensure_views = getattr(self._bundle, "ensure_views", None)
        if callable(ensure_views):
            ensure_views()
        else:
            ensure_runtime = getattr(self._bundle, "ensure_runtime", None)
            if callable(ensure_runtime):
                ensure_runtime()
        files = [f for f in meta.get("files") or [] if isinstance(f, str)]
        query = " ".join(
            [meta.get("title", ""), meta.get("summary", "")]
            + (meta.get("keywords") or [])
            + files
        ).strip()
        pool_k = max(top_k, top_k * 4)
        store = self._bundle.vector_store
        nodes: List[Any] = []
        try:
            if store is not None and hasattr(store, "search_with_content"):
                nodes = store.search_with_content(query, top_k=pool_k)
                return self._rerank_for_page(meta, nodes)[:top_k]
            if store is not None:
                nodes = store.search(query, top_k=pool_k)
                return self._rerank_for_page(meta, nodes)[:top_k]
        except Exception as exc:  # noqa: BLE001 - fall back to BM25 below
            logger.warning("wiki retrieve (vector) failed: %s", exc)
        bm25 = self._bundle.bm25
        if bm25 is not None:
            try:
                nodes = bm25.search(query, pool_k)
                return self._rerank_for_page(meta, nodes)[:top_k]
            except Exception:  # noqa: BLE001
                return []
        return []

    @staticmethod
    def _norm_hint_path(path: str) -> str:
        return path.replace("\\", "/").strip().strip("/")

    @staticmethod
    def _path_matches_hint(file: str, hint: str) -> bool:
        if not file or not hint:
            return False
        hint = hint.rstrip("/")
        return (
            file == hint
            or file.endswith("/" + hint)
            or file.startswith(hint + "/")
            or ("/" + hint + "/") in ("/" + file)
        )

    @staticmethod
    def _keyword_terms(keywords: List[str]) -> List[str]:
        terms: List[str] = []
        seen = set()
        for keyword in keywords:
            for term in re.findall(r"[a-zA-Z0-9_]{3,}", keyword.lower()):
                if term not in seen:
                    seen.add(term)
                    terms.append(term)
        return terms

    def _candidate_hint_score(
        self, meta: Dict[str, Any], node: Any, file_hints: List[str], terms: List[str]
    ) -> float:
        file = self._norm_hint_path(self._rel(self._node_attr(node, "file")) or "")
        name = self._node_attr(node, "node_name") or self._node_attr(node, "name") or ""
        content = (self._node_attr(node, "content") or "")[:1200]
        haystack = f"{file} {name} {content}".lower()
        score = 0.0

        for hint in file_hints:
            if self._path_matches_hint(file, hint):
                score += 5.0
            else:
                base = hint.rsplit("/", 1)[-1]
                if base and base in file:
                    score += 1.0

        phrase_hits = 0
        for keyword in meta.get("keywords") or []:
            phrase = str(keyword).strip().lower()
            if len(phrase) >= 3 and phrase in haystack:
                phrase_hits += 1
        score += min(4.0, phrase_hits * 2.0)

        term_hits = sum(1 for term in terms if term in haystack)
        score += min(3.0, term_hits * 0.5)
        return score

    def _rerank_for_page(self, meta: Dict[str, Any], nodes: List[Any]) -> List[Any]:
        file_hints = [
            self._norm_hint_path(f)
            for f in meta.get("files") or []
            if isinstance(f, str) and f.strip()
        ]
        terms = self._keyword_terms(
            [str(k) for k in meta.get("keywords") or [] if str(k).strip()]
        )
        if not file_hints and not terms:
            return nodes

        scored: List[tuple[float, int, Any]] = []
        seen = set()
        for i, node in enumerate(nodes):
            file = self._norm_hint_path(self._rel(self._node_attr(node, "file")) or "")
            key = (
                file,
                self._node_attr(node, "start_line"),
                self._node_attr(node, "end_line"),
                self._node_attr(node, "node_name")
                or self._node_attr(node, "name")
                or "",
            )
            if key in seen:
                continue
            seen.add(key)
            scored.append(
                (self._candidate_hint_score(meta, node, file_hints, terms), i, node)
            )
        if any(score > 0 for score, _, _ in scored):
            scored.sort(key=lambda item: (-item[0], item[1]))
        return [node for _, _, node in scored]

    def _node_attr(self, node: Any, key: str, default: Any = None) -> Any:
        if isinstance(node, dict):
            return node.get(key, default)
        return getattr(node, key, default)

    def _context(self, nodes: List[Any]) -> str:
        parts: List[str] = []
        total = 0
        for n in nodes:
            file = self._node_attr(n, "file") or ""
            name = self._node_attr(n, "node_name") or self._node_attr(n, "name") or ""
            content = self._node_attr(n, "content") or ""
            if not content and file:
                start = self._node_attr(n, "start_line")
                end = self._node_attr(n, "end_line")
                rel = self._rel(file)
                src = self._wb.source(rel, start, end) if start is not None else None
                content = (src or {}).get("content", "") if src else ""
            content = (content or "")[:1600]
            if not content:
                continue
            block = f"### {name or file}\n```{_lang(file)}\n{content}\n```\n"
            if total + len(block) > _MAX_CONTEXT_CHARS:
                break
            parts.append(block)
            total += len(block)
        return "\n".join(parts)

    def _rel(self, file: Optional[str]) -> Optional[str]:
        """Normalize an index path to repo-relative — the longest suffix that
        exists under the repo dir (handles ``.codenib/`` and ``/repo/`` roots
        alike). The resolved prefix is cached after the first lookup."""
        if not file:
            return file
        p = file.replace("\\", "/")
        root = getattr(self, "_iroot", None)
        if root is not None:
            return p[len(root) :] if root and p.startswith(root) else p.lstrip("/")
        repo_dir = getattr(self._bundle.entry, "repo_dir", "") or ""
        if repo_dir:
            rd = repo_dir.replace("\\", "/").rstrip("/") + "/"
            if p.startswith(rd):
                self._iroot = rd
                return p[len(rd) :]
            parts = [x for x in p.split("/") if x]
            for i in range(len(parts)):
                rel = "/".join(parts[i:])
                if os.path.exists(os.path.join(repo_dir, rel)):
                    self._iroot = p[: len(p) - len(rel)]
                    return rel
        mi = p.rfind("/repo/")
        if mi != -1:
            self._iroot = p[: mi + 6]
            return p[mi + 6 :]
        self._iroot = ""
        return p.lstrip("/")

    def _citation(self, node: Any) -> dict:
        start = self._node_attr(node, "start_line")
        end = self._node_attr(node, "end_line")
        return {
            "file": self._rel(self._node_attr(node, "file")),
            "start_line": (start + 1) if isinstance(start, int) else None,
            "end_line": (end + 1) if isinstance(end, int) else None,
            "node_name": self._node_attr(node, "node_name")
            or self._node_attr(node, "name")
            or "",
            "type": self._node_attr(node, "type") or "",
            "score": self._node_attr(node, "score"),
            "content": None,
        }

    def _narrate(self, meta: Dict[str, Any], context: str) -> str:
        prompt = _PAGE_PROMPT.format(
            repo=getattr(self._bundle.entry, "repo", "this repository"),
            title=meta.get("title", ""),
            summary=meta.get("summary", ""),
            context=context or "(no code retrieved)",
        )
        try:
            import litellm

            resp = litellm.completion(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2200,
                temperature=0.2,
                **_no_thinking_kwargs(self._model),
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as exc:  # noqa: BLE001 - fail soft to a stub page
            logger.warning("wiki page narration failed: %s", exc)
            return f"_{meta.get('summary', '')}_\n\n(Generation unavailable.)"

    def _architecture_diagram(self, nodes: List[Any]) -> str:
        """A deterministic, guaranteed-valid Mermaid diagram from the symbol
        graph, centered on the page's most relevant resolvable symbol (reuses
        the codemap builder rather than trusting LLM-written Mermaid)."""
        try:
            graph = self._bundle.code_graph()
        except Exception:  # noqa: BLE001
            graph = None
        if graph is None or not nodes:
            return ""
        try:
            from ..web.codemap import build_codemap
        except Exception:  # noqa: BLE001
            return ""
        for n in nodes:
            name = self._node_attr(n, "node_name") or self._node_attr(n, "name")
            if not name:
                continue
            try:
                res = build_codemap(
                    graph, symbol=name, direction="both", depth=1, max_nodes=12
                )
            except Exception:  # noqa: BLE001
                continue
            if res.get("available") and res.get("edges"):
                return "## Architecture\n\n```mermaid\n" + res["mermaid"] + "\n```"
        return ""

    def _generate_page(self, meta: Dict[str, Any]) -> dict:
        nodes = self._retrieve(meta)
        markdown = self._narrate(meta, self._context(nodes))
        diagram = self._architecture_diagram(nodes)
        if diagram:
            markdown = f"{markdown}\n\n{diagram}"
        # Dedup citations by (file, start, end).
        citations: List[dict] = []
        seen = set()
        for n in nodes:
            c = self._citation(n)
            key = (c["file"], c["start_line"], c["end_line"])
            if c["file"] and key not in seen:
                seen.add(key)
                citations.append(c)
        return {
            "id": meta["id"],
            "title": meta.get("title", meta["id"]),
            "markdown": f"# {meta.get('title', '')}\n\n{markdown}",
            "citations": citations,
            "diagram": "",
        }

    # -- passthrough -------------------------------------------------------

    def source(self, file: str, start: Optional[int] = None, end: Optional[int] = None):
        return self._wb.source(file, start, end)
