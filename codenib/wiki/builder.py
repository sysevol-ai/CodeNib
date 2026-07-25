# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Build a repo wiki (page tree + source-grounded page content) from indexes.

Strategy (no LLM): enumerate symbol chunks from the vector store (L2) — or the
BM25 index as a fallback — group them by top-level module (directory), and
template DeepWiki-style pages. Each code reference is a real symbol span; line
numbers are converted from the indexes' 0-based convention to 1-based output.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .narrator import Narrator

# Symbol chunk types worth surfacing as "components" (mirrors SYMBOL_CHUNK_TYPES).
_CONTAINER_TYPES = {
    "class",
    "struct",
    "interface",
    "trait",
    "impl",
    "enum",
    "type",
    "object",
}
_CALLABLE_TYPES = {"function", "method"}
_SYMBOL_TYPES = _CONTAINER_TYPES | _CALLABLE_TYPES

_LANG_FENCE = {
    "python": "python",
    "rust": "rust",
    "go": "go",
    "typescript": "typescript",
    "javascript": "javascript",
    "typescript/javascript": "typescript",
    "c++": "cpp",
    "c": "c",
}

_MAX_MODULES = 8
_MAX_SYMBOLS_PER_PAGE = 12
_MAX_SNIPPET_LINES = 28


@dataclass
class Symbol:
    file: str
    name: str
    type: str
    start_line: int  # 0-based, inclusive (as stored)
    end_line: int
    content: str

    @property
    def lines(self) -> int:
        return max(1, self.end_line - self.start_line + 1)


@dataclass
class PageRef:
    id: str
    title: str
    children: List["PageRef"] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "children": [c.to_dict() for c in self.children],
        }


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _fence(language: str) -> str:
    return _LANG_FENCE.get((language or "").lower(), "")


def _top_module(path: str) -> str:
    parts = [p for p in (path or "").split("/") if p]
    if not parts:
        return "root"
    # Group at depth 2 (e.g. "astropy/io", "lib/core") so single-package repos
    # don't collapse into one giant module; depth 1 when that's all there is.
    return "/".join(parts[:2]) if len(parts) >= 2 else parts[0]


class WikiBuilder:
    """Build wiki structure + content for one loaded repo bundle."""

    def __init__(self, bundle, narrator: Optional[Narrator] = None) -> None:
        # bundle: codenib.web.repo_registry.RepoBundle
        self._bundle = bundle
        self._entry = bundle.entry
        self._language = bundle.entry.language
        self._narrator = narrator or Narrator(enabled=False)
        self._symbols_cache: Optional[tuple] = None

    def _key(self, page_id: str) -> str:
        """Stable cache key for narrated prose: repo@commit/page."""
        return f"{self._entry.repo}@{self._entry.commit_short}/{page_id}"

    @staticmethod
    def _docline(content: str) -> str:
        """First line(s) of a symbol's docstring / leading comment, for prose
        grounding only (never rendered as code)."""
        if not content:
            return ""
        lines = content.splitlines()
        # Python triple-quote docstring.
        for i, ln in enumerate(lines[:6]):
            m = re.search(r'("""|\'\'\')(.*)', ln)
            if m:
                rest = m.group(2).strip()
                if rest and not rest.endswith(('"""', "'''")):
                    # gather until closing or 3 lines
                    buf = [rest]
                    for nxt in lines[i + 1 : i + 4]:
                        if '"""' in nxt or "'''" in nxt:
                            buf.append(nxt.split('"""')[0].split("'''")[0])
                            break
                        buf.append(nxt.strip())
                    return " ".join(x for x in buf if x).strip()[:300]
                return rest.strip("\"' ")[:300]
        # Leading // or # comment lines.
        comment = []
        for ln in lines[:5]:
            s = ln.strip()
            if s.startswith(("//", "#", "*", "///")):
                comment.append(s.lstrip("/#* ").strip())
            elif comment:
                break
        return " ".join(comment)[:300]

    def _class_facts(self, syms: List[Symbol], limit: int = 12) -> List[tuple]:
        """(class_name, docstring) for the largest classes in a symbol set."""
        _, classes, _ = self._classify(syms)
        ranked = sorted(
            classes.items(), key=lambda kv: sum(m.lines for m in kv[1]), reverse=True
        )[:limit]
        out = []
        for cls, methods in ranked:
            rep = max(methods, key=lambda m: m.lines) if methods else None
            out.append((cls, self._docline(rep.content) if rep else ""))
        return out

    # -- symbol enumeration ------------------------------------------------

    def _index_root(self, sample: str) -> str:
        """Derive the absolute prefix to strip so paths become repo-relative.

        Index metadata stores build-machine-absolute paths; the repo-relative
        path is the longest suffix of ``sample`` that exists under repo_dir.
        """
        repo_dir = os.path.abspath(self._entry.repo_dir)
        parts = [p for p in sample.split("/") if p]
        for i in range(len(parts)):
            rel = "/".join(parts[i:])
            if os.path.exists(os.path.join(repo_dir, rel)):
                return "/".join(sample.split("/")[: sample.split("/").index(parts[i])])
        return os.path.dirname(sample)

    def _symbols(self) -> tuple:
        """Memoized symbol enumeration (one builder lives per repo)."""
        if self._symbols_cache is None:
            self._symbols_cache = self._compute_symbols()
        return self._symbols_cache

    def _compute_symbols(self) -> tuple:
        ensure_runtime = getattr(self._bundle, "ensure_runtime", None)
        if callable(ensure_runtime):
            ensure_runtime()
        vs = getattr(self._bundle, "vector_store", None)
        docs = list(getattr(vs, "l2_documents", []) or []) if vs is not None else []
        if not docs:
            bm25 = getattr(self._bundle, "bm25", None)
            docs = (
                list(getattr(bm25, "documents", []) or []) if bm25 is not None else []
            )
        # First pass: collect raw (absolute) records.
        raw = []
        for d in docs:
            md = getattr(d, "metadata", {}) or {}
            ctype = (md.get("chunk_type") or md.get("type") or "").lower()
            if ctype not in _SYMBOL_TYPES:
                continue
            name = md.get("name")
            f = md.get("file")
            if not name or not f:
                continue
            raw.append(
                (
                    f,
                    str(name),
                    ctype,
                    int(md.get("start_line", 0) or 0),
                    int(md.get("end_line", 0) or 0),
                    getattr(d, "page_content", "") or "",
                )
            )
        if not raw:
            return tuple()
        root = self._index_root(raw[0][0])
        prefix = (root + "/") if root else ""
        syms = [
            Symbol(
                file=(
                    f[len(prefix) :]
                    if prefix and f.startswith(prefix)
                    else f.lstrip("/")
                ),
                name=n,
                type=t,
                start_line=s,
                end_line=e,
                content=c,
            )
            for (f, n, t, s, e, c) in raw
        ]
        return tuple(syms)

    def _modules(self) -> Dict[str, List[Symbol]]:
        groups: Dict[str, List[Symbol]] = {}
        for s in self._symbols():
            groups.setdefault(_top_module(s.file), []).append(s)
        return groups

    def _ranked_modules(self) -> List[str]:
        groups = self._modules()
        return sorted(groups, key=lambda m: len(groups[m]), reverse=True)[:_MAX_MODULES]

    # -- page tree ---------------------------------------------------------

    def page_tree(self) -> List[dict]:
        pages = [PageRef(id="overview", title="Overview")]
        arch = PageRef(id="architecture", title="Architecture")
        for m in self._ranked_modules():
            arch.children.append(PageRef(id=f"mod__{_slug(m)}", title=m))
        pages.append(arch)
        return [p.to_dict() for p in pages]

    # -- page content ------------------------------------------------------

    def _citation(self, s: Symbol) -> dict:
        snippet = "\n".join(s.content.splitlines()[:_MAX_SNIPPET_LINES])
        return {
            "file": s.file,
            "start_line": s.start_line + 1,  # 0-based -> 1-based output
            "end_line": s.end_line + 1,
            "node_name": s.name,
            "type": s.type,
            "content": snippet,
        }

    def _salient_files(self, limit: int = 12) -> List[str]:
        """Rank files by indexed API surface (total symbol lines), so the
        overview surfaces real entry points rather than an alphabetical slice
        of `__init__.py`/`conftest.py`."""
        weight: Dict[str, int] = {}
        for s in self._symbols():
            weight[s.file] = weight.get(s.file, 0) + s.lines
        return sorted(weight, key=lambda f: weight[f], reverse=True)[:limit]

    def _overview_md(self) -> tuple:
        groups = self._modules()
        mods = self._ranked_modules()
        total_syms = len(self._symbols())
        n_files = len({s.file for s in self._symbols()})
        files = self._salient_files(12)
        lang = self._language or "code"
        # LLM-authored narrative lead (DeepWiki style); falls back to the
        # templated factual sentence when no model/creds are available (G6).
        highlights = [
            f"{c} — {d}" if d else c
            for c, d in self._class_facts(groups.get(mods[0], []) if mods else [], 10)
        ]
        narrative = self._narrator.overview(
            self._entry.repo,
            lang,
            [(m, len(groups[m])) for m in mods],
            highlights,
            self._key("overview"),
        )
        lines = [f"# {self._entry.repo} Overview", ""]
        if narrative:
            lines += [narrative, ""]
        else:
            commit = (
                f" at commit `{self._entry.commit_short}`"
                if self._entry.commit_short
                else ""
            )
            lines += [
                f"`{self._entry.repo}` is indexed{commit} ({lang}). "
                "This wiki is generated from "
                f"CodeNib's static analysis of **{n_files} source files** and "
                f"**{total_syms} symbols**, grouped into the modules below.",
                "",
            ]
        lines += [
            "## Module structure",
            "",
            (
                "The repository's top-level modules and their relative size "
                + "(by indexed symbol count):"
            ),
            "",
        ]
        # Mermaid diagram of the top modules. Use .format so the literal quotes
        # (Mermaid node syntax) aren't flagged as a !r candidate (flake8 B907).
        diagram = [
            "graph TD",
            '  ROOT["{}"]'.format(self._entry.repo),
        ]
        for i, m in enumerate(mods):
            nid = f"M{i}"
            diagram.append(f'  ROOT --> {nid}["{m}<br/>{len(groups[m])} symbols"]')
        diagram_str = "\n".join(diagram)
        lines += ["```mermaid", diagram_str, "```", ""]
        lines += ["## Modules", ""]
        for m in mods:
            lines.append(
                f"- [**{m}**](?p=mod__{_slug(m)}) — {len(groups[m])} indexed symbols"
            )
        lines += ["", "## Relevant source files", ""]
        for f in files[:12]:
            lines.append(f"- `{f}`")
        # NB: no "Example issue" / problem_statement here — that is a SWE-bench
        # dataset artifact, not repo documentation (Critique #8, G3).
        # A couple of citations from the largest module.
        cites = []
        if mods:
            for s in sorted(groups[mods[0]], key=lambda x: x.lines, reverse=True)[:3]:
                cites.append(self._citation(s))
        return "\n".join(lines), cites, diagram_str

    def _classify(self, syms: List[Symbol]):
        """Group symbols into synthesized classes (by `Class.method` prefix)
        plus standalone functions. L2 docs are method-level, so classes are
        recovered from name prefixes."""
        explicit = {s.name: s for s in syms if s.type in _CONTAINER_TYPES}
        classes: Dict[str, List[Symbol]] = {n: [] for n in explicit}
        funcs: List[Symbol] = []
        for s in syms:
            if s.type in _CONTAINER_TYPES:
                continue
            if "." in s.name:
                classes.setdefault(s.name.split(".")[0], []).append(s)
            else:
                funcs.append(s)
        return explicit, classes, funcs

    def _module_md(self, module: str) -> tuple:
        groups = self._modules()
        syms = groups.get(module, [])
        explicit, classes, funcs = self._classify(syms)
        files = sorted({s.file for s in syms})
        fence = _fence(self._language)
        n_types = len(set(explicit) | set(classes))
        fact = (
            f"The `{module}` module contains **{len(syms)} indexed symbols** "
            f"across **{len(files)} files** "
            f"({n_types} types, {len(funcs)} top-level functions)."
        )
        # LLM-authored module intro leads (G4); the structural fact line is kept
        # below it (and remains the lead in the templated fallback).
        intro = self._narrator.module_intro(
            self._entry.repo,
            module,
            files,
            [f"{c} — {d}" if d else c for c, d in self._class_facts(syms, 10)],
            self._key("mod__" + _slug(module)),
        )
        lines = [f"# {module}", ""]
        if intro:
            lines += [intro, "", fact, ""]
        else:
            lines += [fact, ""]
        lines += ["## Files", ""]
        diagram = ["graph TD", '  MOD["{}"]'.format(module)]
        for i, f in enumerate(files[:10]):
            diagram.append('  MOD --> F{}["{}"]'.format(i, f.split("/")[-1]))
        diagram_str = "\n".join(diagram)
        lines += ["```mermaid", diagram_str, "```", ""]
        for f in files[:12]:
            lines.append(f"- `{f}`")

        cites = []
        # Rank classes by total method size; keep the largest few.
        ranked_classes = sorted(
            classes.items(), key=lambda kv: sum(m.lines for m in kv[1]), reverse=True
        )[: max(1, _MAX_SYMBOLS_PER_PAGE // 2)]
        # Batch one LLM call for all component descriptions (G2).
        descriptions = (
            self._narrator.components(
                self._entry.repo,
                module,
                [
                    (
                        cls,
                        self._docline(
                            (max(ms, key=lambda m: m.lines).content if ms else "")
                        ),
                    )
                    for cls, ms in ranked_classes
                ],
                self._key("mod__" + _slug(module)),
            )
            or {}
        )
        lines += ["", "## Key components", ""]
        first_anchor = None
        for cls, methods in ranked_classes:
            # Heading id is supplied by rehype-slug on the frontend; _slug(cls)
            # matches its output, so the jump link below resolves.
            first_anchor = first_anchor or _slug(cls)
            rep = max(methods, key=lambda m: m.lines) if methods else None
            file = (
                rep.file
                if rep
                else (explicit.get(cls).file if explicit.get(cls) else "")
            )
            desc = descriptions.get(cls)
            method_line = (
                f"Type in `{file}` with **{len(methods)} indexed methods**: "
                + ", ".join(f"`{m.name.split('.')[-1]}`" for m in methods[:8])
                + ("…" if len(methods) > 8 else "")
                + "."
            )
            lines += [f"### `{cls}`", ""]
            if desc:
                # LLM responsibility prose leads; the method roster follows as a
                # smaller factual note (so real symbols stay visible).
                lines += [desc, "", method_line, ""]
            else:
                lines += [method_line, ""]
            if rep and rep.content.strip():
                snippet = "\n".join(rep.content.splitlines()[:_MAX_SNIPPET_LINES])
                lines += [
                    f"Representative method `{rep.name.split('.')[-1]}` "
                    f"(lines {rep.start_line + 1}–{rep.end_line + 1}):",
                    "",
                    f"```{fence}",
                    snippet,
                    "```",
                    "",
                ]
                cites.append(self._citation(rep))
        # A few standalone functions.
        if funcs:
            lines += ["## Functions", ""]
            for s in sorted(funcs, key=lambda x: x.lines, reverse=True)[:5]:
                lines.append(
                    f"- `{s.name}` — `{s.file}` (lines {s.start_line + 1}–{s.end_line + 1})"
                )
                cites.append(self._citation(s))

        # Related modules — cross-page links (DeepWiki-style navigation).
        siblings = [m for m in self._ranked_modules() if m != module][:5]
        if siblings:
            lines += ["", "## Related modules", ""]
            for m in siblings:
                lines.append(f"- [{m}](?p=mod__{_slug(m)})")

        if first_anchor and "## Files" in lines:
            at = lines.index("## Files")
            lines[at:at] = [
                f"Jump to the primary component below: [view](#{first_anchor}).",
                "",
            ]
        return "\n".join(lines), cites, diagram_str

    def page(self, page_id: str) -> Optional[dict]:
        if page_id == "overview":
            md, cites, diagram = self._overview_md()
            return {
                "id": "overview",
                "title": "Overview",
                "markdown": md,
                "citations": cites,
                "diagram": diagram,
            }
        if page_id == "architecture":
            mods = self._ranked_modules()
            md = [
                "# Architecture",
                "",
                (
                    "CodeNib grouped this repository into the following modules. "
                    + "Select a module in the sidebar to explore its components."
                ),
                "",
                "## Modules",
                "",
            ]
            for m in mods:
                md.append(f"- **{m}**")
            _, _, diagram = self._overview_md()
            return {
                "id": "architecture",
                "title": "Architecture",
                "markdown": "\n".join(md),
                "citations": [],
                "diagram": diagram,
            }
        if page_id.startswith("mod__"):
            target = page_id[len("mod__") :]
            for m in self._ranked_modules():
                if _slug(m) == target:
                    md, cites, diagram = self._module_md(m)
                    return {
                        "id": page_id,
                        "title": m,
                        "markdown": md,
                        "citations": cites,
                        "diagram": diagram,
                    }
        return None

    # -- raw source --------------------------------------------------------

    def source(
        self, file: str, start: Optional[int] = None, end: Optional[int] = None
    ) -> Optional[dict]:
        repo_dir = self._entry.repo_dir
        # Prevent path traversal; only serve files under repo_dir.
        safe = os.path.normpath(file).lstrip("/")
        abs_path = os.path.normpath(os.path.join(repo_dir, safe))
        if not abs_path.startswith(os.path.abspath(repo_dir)) or not os.path.isfile(
            abs_path
        ):
            return None
        with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
            all_lines = fh.readlines()
        if start is not None and end is not None:
            chunk = all_lines[max(0, start - 1) : end]  # 1-based inclusive
            first = max(1, start)
        else:
            chunk = all_lines[:400]
            first = 1
        return {
            "file": safe,
            "start_line": first,
            "end_line": first + len(chunk) - 1,
            "content": "".join(chunk),
        }
