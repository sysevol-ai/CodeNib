# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""One repository's index, read the way a walkthrough needs it.

A walkthrough moves from function to function along calls. This module reads
what those moves rest on: the source lines, the calls the index recorded at
each line, the classes and their bases (for a call into a base method that a
subclass implements), and the few calls the index misses that the source
still settles, such as ``self.executor(...)`` where the attribute's type is
written in the class.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from codenib.web.config import QAConfig
from codenib.web.repo_registry import RepoRegistry
from codenib.wiki.agent_wiki import AgentWiki
from codenib.wiki.sqlite_store import SQLiteWikiStore

CALLABLE = {"function", "method"}
# Calls a reader of the source would not follow: builtins and container methods.
SKIP_CALLS = {
    "print",
    "len",
    "isinstance",
    "str",
    "dict",
    "list",
    "set",
    "tuple",
    "int",
    "float",
    "bool",
    "getattr",
    "setattr",
    "hasattr",
    "super",
    "range",
    "enumerate",
    "zip",
    "sorted",
    "min",
    "max",
    "any",
    "all",
    "type",
    "repr",
    "iter",
    "next",
    "open",
    "format",
    "join",
    "append",
    "get",
    "update",
    "items",
    "keys",
    "values",
    "strip",
    "split",
}


def open_registry(cfg: QAConfig) -> RepoRegistry:
    """Every indexed repository the config's registry lists, loaded once."""

    registry = RepoRegistry(cfg, allow_missing_native_index_authorization=True)
    registry.load_all()
    return registry


def load_repo_index(
    cfg: QAConfig, repo_id: str, registry: RepoRegistry | None = None
) -> "RepoIndex":
    """Open one repository's graph, source and cached Wiki outline."""

    registry = registry or open_registry(cfg)
    bundle = registry.get(repo_id)
    store = SQLiteWikiStore(
        Path(os.path.abspath(cfg.data_dir)) / "wiki_cache" / "wiki.sqlite3"
    )
    wiki = AgentWiki(bundle, cfg.wiki_generation_model, store=store)
    return RepoIndex(repo_id, bundle, wiki)


class RepoIndex:
    """A repository's call graph and source, with the reads a walkthrough makes.

    A function is described by :meth:`info` as a dict: ``vid`` (graph vertex),
    ``symbol``, ``file`` (repository-relative), ``name`` (``Class.method()``),
    ``a``/``b`` (first and last line, 1-based) and ``kind``.
    """

    def __init__(self, repo_id: str, bundle, wiki: AgentWiki) -> None:
        self.repo_id = repo_id
        self.bundle = bundle
        self.wiki = wiki
        self.outline = wiki.cached_outline()
        if not self.outline:
            raise ValueError(f"{repo_id} has no cached Wiki outline")
        self.graph = wiki._bundle.code_graph()
        self.raw = self.graph.get_graph()
        self.names = {
            v["unified_name"]: v.index for v in self.raw.vs if v["unified_name"]
        }
        self.by_name: dict[str, list[int]] = {}
        for sym, vid in self.names.items():
            vertex = self.raw.vs[vid]
            # A callable with no recorded lines has no source to show.
            if str(vertex["type"]).lower() not in CALLABLE:
                continue
            if vertex["start_line"] is None or not vertex["file"]:
                continue
            self.by_name.setdefault(sym.split(":")[-1], []).append(vid)
        self._files: dict[str, list[str]] = {}
        self._neighbors: dict[int, list] = {}
        self.classes = self._read_classes()

    @property
    def commit(self) -> str:
        entry = getattr(self.bundle, "entry", None)
        commit = getattr(entry, "base_commit", None) or getattr(
            entry, "commit_short", None
        )
        return (commit or "").strip()

    # ---------- source ----------

    def lines_of(self, rel: str) -> list[str]:
        if rel not in self._files:
            got = self.wiki._wb.source(rel, 1, 100000) or {}
            self._files[rel] = (got.get("content") or "").split("\n")
        return self._files[rel]

    def line(self, rel: str, n: int) -> str:
        ls = self.lines_of(rel)
        return ls[n - 1] if 0 < n <= len(ls) else ""

    def numbered(self, rel: str, a: int, b: int, limit: int = 140) -> str:
        out = []
        for n in range(a, b + 1):
            out.append(f"{n:5d}  {self.line(rel, n)}")
        if len(out) > limit:
            out = out[: limit - 20] + ["  ...  (lines omitted)"] + out[-19:]
        return "\n".join(out)

    # ---------- graph ----------

    def info(self, vid: int) -> dict:
        v = self.raw.vs[vid]
        rel = self.wiki._rel(v["file"])
        name = v["unified_name"].split(":")[-1]
        return {
            "vid": vid,
            "symbol": v["unified_name"],
            "file": rel,
            "name": name,
            "a": v["start_line"] + 1,
            "b": v["end_line"] + 1,
            "kind": str(v["type"]).lower(),
        }

    def out_calls(self, vid: int) -> list[dict]:
        """The recorded calls a function makes, by line: ``{"line", "vid"}``."""

        f = self.info(vid)
        try:
            res = self.graph.query_range(f["file"], f["a"] - 1, f["b"] - 1)
        except Exception:
            return []
        seen, out = set(), []
        for e in res.outgoing:
            t = self.raw.vs[e.target_vid]
            if (
                e.anchor_line is None
                or str(t["type"]).lower() not in CALLABLE
                or e.target_vid == vid
                or t["start_line"] is None  # named, but not placed in a file
                or not t["file"]
            ):
                continue
            key = (e.target_vid, e.anchor_line)
            if key in seen:
                continue
            seen.add(key)
            out.append({"line": e.anchor_line + 1, "vid": e.target_vid})
        return sorted(out, key=lambda c: c["line"])

    def neighbors(self, vid: int) -> list[tuple]:
        """Where a function leads: ``(vid, line, kind)``, cached.

        ``kind`` is "call" (recorded), "subclass" (an abstract method's
        implementations) or "unrecorded" (an attribute call resolved from
        the source; see :meth:`resolve_attr_call`).
        """

        if vid not in self._neighbors:
            self._neighbors[vid] = self._find_neighbors(vid)
        return self._neighbors[vid]

    def _find_neighbors(self, vid: int) -> list[tuple]:
        f = self.info(vid)
        calls = self.out_calls(vid)
        out = [(c["vid"], c["line"], "call") for c in calls]
        if self.is_abstract(f):
            out += [(i, None, "subclass") for i in self.overrides(vid)]
        recorded = {c["line"] for c in calls}
        for n in range(f["a"] + 1, f["b"] + 1):
            if n not in recorded:
                v2 = self.resolve_attr_call(f, n)
                if v2 is not None:
                    out.append((v2, n, "unrecorded"))
        return out

    def find_symbol(self, name: str) -> int | None:
        """The one callable whose name ends with ``name``, or None."""

        name = name.strip().rstrip(";")
        if not name.endswith(")"):
            name += "()"
        hits = [
            i
            for s, i in self.names.items()
            if s.endswith(":" + name)
            and str(self.raw.vs[i]["type"]).lower() in CALLABLE
        ]
        return hits[0] if len(hits) == 1 else None

    # ---------- classes ----------

    def _read_classes(self) -> dict[str, list[dict]]:
        """Classes by simple name, with their bases read from the class line."""

        classes: dict[str, list[dict]] = {}
        for v in self.raw.vs:
            if str(v["type"]).lower() != "class" or not v["unified_name"]:
                continue
            if v["start_line"] is None or not v["file"]:
                continue  # a class the index names but does not place
            rel = self.wiki._rel(v["file"])
            if not rel.endswith((".py", ".pyi")):
                continue  # bases are read from Python's class line only
            head, k = self.line(rel, v["start_line"] + 1), v["start_line"] + 1
            while (
                "(" in head
                and head.count("(") > head.count(")")
                and k < v["start_line"] + 12
            ):
                k += 1
                head += " " + self.line(rel, k).strip()
            m = re.match(r"\s*class\s+(\w+)\s*(?:\(([^)]*)\))?", head)
            if m:
                bases = [
                    b.strip().split(".")[-1].split("[")[0]
                    for b in (m.group(2) or "").split(",")
                    if b.strip()
                ]
                classes.setdefault(m.group(1), []).append(
                    {
                        "file": rel,
                        "bases": bases,
                        "a": v["start_line"] + 1,
                        "b": v["end_line"] + 1,
                    }
                )
        return classes

    def subclasses(self, cls: str) -> set[str]:
        out, todo = set(), [cls]
        while todo:
            c = todo.pop()
            for name, defs in self.classes.items():
                if any(c in d["bases"] for d in defs) and name not in out:
                    out.add(name)
                    todo.append(name)
        return out

    def ancestors(self, cls: str) -> list[str]:
        out, todo = [], [cls]
        while todo:
            c = todo.pop(0)
            for d in self.classes.get(c, []):
                for b in d["bases"]:
                    if b not in out and b in self.classes:
                        out.append(b)
                        todo.append(b)
        return out

    def overrides(self, vid: int) -> list[int]:
        """The same method on every subclass of the method's class."""

        f = self.info(vid)
        if "." not in f["name"]:
            return []
        cls, meth = f["name"].split(".", 1)
        out = []
        for sub in sorted(self.subclasses(cls)):
            out.extend(self.by_name.get(f"{sub}.{meth}", []))
        return out

    def method_vid(self, cls: str, meth: str) -> int | None:
        """``cls.meth()`` as defined on the class or the nearest ancestor."""

        for c in [cls] + self.ancestors(cls):
            hits = self.by_name.get(f"{c}.{meth}()")
            if hits:
                return hits[0]
        return None

    def type_name(self, t: str | None) -> str | None:
        """A type annotation's class name, when the class is in the repository."""

        t = (t or "").strip().strip("'\"")
        t = re.sub(r"^(Optional|typing\.Optional)\[(.*)\]$", r"\2", t)
        t = t.split("|")[0].strip().split("[")[0].split(".")[-1]
        return t if t in self.classes else None

    # ---------- reading a function ----------

    def is_abstract(self, f: dict) -> bool:
        line = self.line
        head = [
            line(f["file"], n).strip() for n in range(max(1, f["a"] - 2), f["a"] + 1)
        ]
        if any(
            h.startswith("@abstractmethod") or h.startswith("@abc.abstractmethod")
            for h in head
        ):
            return True
        if re.search(r":\s*(\.\.\.|pass)\s*$", line(f["file"], f["a"])):
            return True
        body = [line(f["file"], n).strip() for n in range(f["a"] + 1, f["b"] + 1)]
        body = [
            b
            for b in body
            if b and not b.startswith(("#", '"', "'", "@")) and not b.startswith(":")
        ]
        return any("raise NotImplementedError" in b for b in body) or all(
            b in ("pass", "...") for b in body
        )

    def unresolved(self, f: dict, recorded_lines: set[int]) -> list[str]:
        """Lines with a call the index did not record, at most ten."""

        out = []
        for n in range(f["a"] + 1, f["b"] + 1):
            if n in recorded_lines:
                continue
            txt = self.line(f["file"], n)
            code = txt.split("#")[0]
            if code.strip().startswith(("def ", "class ", "@", '"', "'")):
                continue
            for m in re.finditer(r"((?:self\.)?[A-Za-z_][\w.]*)\(", code):
                name = m.group(1).split(".")[-1]
                if name in SKIP_CALLS or (
                    name.startswith("_") and not m.group(1).startswith("self.")
                ):
                    continue
                out.append(f"line {n}: `{code.strip()[:90]}`")
                break
        return out[:10]

    def ahead(self, vid: int) -> str:
        """How much a hop leads to: its size and the functions it calls next."""

        f = self.info(vid)
        nxt = []
        for c in self.out_calls(vid):
            n = self.info(c["vid"])["name"]
            if n not in nxt:
                nxt.append(n)
        subs = (
            [self.info(i)["name"] for i in self.overrides(vid)]
            if self.is_abstract(f)
            else []
        )
        bits = [f"{f['b'] - f['a'] + 1} lines"]
        if subs:
            bits.append("abstract; implemented by " + ", ".join(subs[:4]))
        if nxt:
            more = f" and {len(nxt) - 6} more" if len(nxt) > 6 else ""
            bits.append("calls " + ", ".join(nxt[:6]) + more)
        else:
            bits.append("calls nothing in the repository")
        return "; ".join(bits)

    def resolve_attr_call(self, cur: dict, n: int) -> int | None:
        """``self.attr(...)`` on line ``n`` -> ``AttrType.__call__``, or None.

        The type comes from the attribute's assignment or annotation in the
        class (or an ancestor): a constructor call, or a method whose return
        annotation names the type. This reads source the index skipped, so
        the hop is labelled as not recorded by the index.
        """

        line = self.line
        m = re.search(r"self\.(\w+)\(", line(cur["file"], n).split("#")[0])
        if not m or "." not in cur["name"]:
            return None
        attr, cls = m.group(1), cur["name"].split(".")[0]
        if self.method_vid(cls, attr) is not None:
            return None
        assign = re.compile(r"self\." + attr + r"\s*(?::\s*([^=]+?))?\s*=\s*(.+)$")
        for c in [cls] + self.ancestors(cls):
            for d in self.classes.get(c, []):
                for k in range(d["a"], d["b"] + 1):
                    a = assign.search(line(d["file"], k))
                    if not a:
                        continue
                    for t in self._assigned_types(c, a):
                        if t:
                            vid = self.method_vid(t, "__call__")
                            if vid is not None:
                                return vid
        return None

    def _assigned_types(self, c: str, a: re.Match) -> list[str | None]:
        """Candidate types of one ``self.attr = ...`` line, in order."""

        cands = []
        if a.group(1):
            cands.append(self.type_name(a.group(1)))
        for part in re.split(r"\bor\b", a.group(2)):
            part = part.strip()
            mm = re.match(r"([A-Z]\w*)\(", part)
            if mm:
                cands.append(self.type_name(mm.group(1)))
            mm = re.match(r"self\.(\w+)\(", part)
            if mm:
                mv = self.method_vid(c, mm.group(1))
                if mv is not None:
                    mf = self.info(mv)
                    sig = " ".join(
                        self.line(mf["file"], q).strip()
                        for q in range(mf["a"], min(mf["b"], mf["a"] + 6) + 1)
                    )
                    r = re.search(r"->\s*([\w.\[\]|' ]+?)\s*:", sig)
                    if r:
                        cands.append(self.type_name(r.group(1)))
        return cands
