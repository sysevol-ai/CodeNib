# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Dense, hybrid, Zoekt, and graph-expansion ARB rankers (labs).

Each index is built once per corpus snapshot and exposes ``build_seconds``,
``size``, ``tool_calls`` (searches issued by the latest ``rank``), and
``rank(query_text)``. They read only the released query text and corpus rows,
plus, for graph expansion, a clean checkout at the sample's base commit; gold,
fix commits, and annotations never reach them.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

import numpy as np

from codenib.types import EDGE_TYPE_CONTAIN, EDGE_TYPE_REFERENCE, NODE_TYPE_FILE

from .agent_retrieval_bench import BM25CorpusIndex, complete_file_ranking, corpus_chunks

DENSE_MODEL = "Qwen/Qwen3-Embedding-0.6B"
DENSE_DIMENSION = 1024
DENSE_MAX_SEQ_LENGTH = 512
DENSE_BATCH_SIZE = 64
HYBRID_RRF_K = 60
ZOEKT_MAX_TERMS = 32
_SYMBOL_DIR = "__codenib_arb_symbols__"


class _CachedEmbedding:
    """Encodes each distinct document text once per run.

    Snapshots of one repository share most files, so 6.2M released chunks
    hold about 0.9M distinct texts. Queries are never cached.
    """

    def __init__(self, embedding: Any):
        self._embedding = embedding
        self._vectors: dict[bytes, np.ndarray] = {}
        self.encoded_documents = 0
        self.reused_documents = 0

    def embed_documents(self, texts: list[str]) -> list[np.ndarray]:
        keys = [hashlib.sha256(text.encode("utf-8")).digest() for text in texts]
        missing = {
            key: text
            for key, text in zip(keys, texts, strict=True)
            if key not in self._vectors
        }
        if missing:
            vectors = self._embedding.embed_documents(list(missing.values()))
            for key, vector in zip(missing, vectors, strict=True):
                # Normalized vectors keep their ranking at half precision.
                self._vectors[key] = np.asarray(vector, dtype=np.float16)
        self.encoded_documents += len(missing)
        self.reused_documents += len(texts) - len(missing)
        return [self._vectors[key].astype(np.float32) for key in keys]

    def embed_query(self, text: str) -> list[float]:
        return self._embedding.embed_query(text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._embedding, name)


class DenseEncoder:
    """One embedding model, loaded once and shared by every snapshot index."""

    def __init__(self, model: str = DENSE_MODEL, device: str | None = None):
        import torch

        from codenib.index.embedding import CodeVectorStore

        device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        started = time.perf_counter()
        self.store_options = {
            "embedding_model": model,
            "embedding_provider": "huggingface",
            "dimension": DENSE_DIMENSION,
            "index_type": "flat",
            "index_metric": "ip",
        }
        template = CodeVectorStore(
            **self.store_options,
            device=device,
            default_batch_size=DENSE_BATCH_SIZE,
            max_seq_length=DENSE_MAX_SEQ_LENGTH,
            # ARB's embedding baseline uses no query or document instruction.
            query_prompt="",
            document_prompt="",
            encode_kwargs={"normalize_embeddings": True},
        )
        dtype = "float32"
        if device.startswith("cuda"):
            template.embedding.client.to(torch.bfloat16)
            dtype = "bfloat16"
        self.embedding = _CachedEmbedding(template.embedding)
        self.load_seconds = time.perf_counter() - started
        self.config = {
            "model": model,
            "device": device,
            "dtype": dtype,
            "max_seq_length": DENSE_MAX_SEQ_LENGTH,
            "batch_size": DENSE_BATCH_SIZE,
            "query_prompt": "",
            "index": "exact inner product over normalized vectors",
        }

    def usage(self) -> dict[str, Any]:
        return {
            "embedding_model": self.config["model"],
            "model_load_seconds": self.load_seconds,
            "encoded_documents": self.embedding.encoded_documents,
            "reused_documents": self.embedding.reused_documents,
        }


class DenseCorpusIndex:
    """Exact dense search over every released chunk, ranked to files."""

    tool_calls = 1

    def __init__(self, corpus: list[dict[str, Any]], encoder: DenseEncoder):
        from codenib.index.embedding import CodeVectorStore

        started = time.perf_counter()
        before = (
            encoder.embedding.encoded_documents,
            encoder.embedding.reused_documents,
        )
        self._paths = {row["path"] for row in corpus}
        self._store = CodeVectorStore(
            **encoder.store_options, embedding=encoder.embedding
        )
        self._store.add_code_chunks(
            [chunk._asdict() for chunk in corpus_chunks(corpus)], level="l2"
        )
        self.build_seconds = time.perf_counter() - started
        documents = self._store.l2_index.ntotal
        self.size = {
            "documents": documents,
            "dimension": self._store.dimension,
            "vector_bytes": documents * self._store.dimension * 4,
            "encoded_documents": encoder.embedding.encoded_documents - before[0],
            "reused_documents": encoder.embedding.reused_documents - before[1],
        }

    def rank(self, query_text: str) -> list[str]:
        nodes = self._store.search(
            query_text, top_k=self._store.l2_index.ntotal, level="l2"
        )
        return complete_file_ranking((node.file for node in nodes), self._paths)


class HybridCorpusIndex:
    """CodeNib's file-level weighted RRF over the BM25 and dense rankings."""

    tool_calls = 2

    def __init__(self, corpus: list[dict[str, Any]], encoder: DenseEncoder):
        self._bm25 = BM25CorpusIndex(corpus)
        self._dense = DenseCorpusIndex(corpus, encoder)
        self._paths = {row["path"] for row in corpus}
        self.build_seconds = self._bm25.build_seconds + self._dense.build_seconds
        self.size = {"bm25": self._bm25.size, "dense": self._dense.size}

    def rank(self, query_text: str) -> list[str]:
        from codenib.ops.retrieve import merge_file_rankings
        from codenib.types import NODE_TYPE_FILE, QueriedNode

        branches = [
            [
                QueriedNode(node_name=path, type=NODE_TYPE_FILE, file=path)
                for path in index.rank(query_text)
            ]
            for index in (self._bm25, self._dense)
        ]
        fused = merge_file_rankings(branches, rrf_k=HYBRID_RRF_K)
        return complete_file_ranking((node.file for node in fused), self._paths)


_BACKTICK = re.compile(r"`([^`\n]{2,60})`")
_PATH = re.compile(r"(?:[\w.@+-]+/)*[\w@+-][\w.@+-]*\.[A-Za-z][A-Za-z0-9]{0,7}\b")
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_CODEISH = re.compile(r"_|[a-z][A-Z]")
_STOP = {"BUG", "TODO", "FIXME", "NOTE", "XXX", "HACK", "WARNING", "ERROR", "FAIL"}


_RE2_META = re.compile(r"([\\.+*?()|\[\]{}^$])")


def _query_values(query_text: str) -> str:
    """The released query's string values; field names are not query terms."""
    try:
        value = json.loads(query_text)
    except ValueError:
        return query_text
    strings: list[str] = []
    pending = [value]
    while pending:
        item = pending.pop(0)
        if isinstance(item, str):
            strings.append(item)
        elif isinstance(item, dict):
            pending.extend(item[key] for key in sorted(item))
        elif isinstance(item, list):
            pending.extend(item)
    return "\n".join(strings)


def zoekt_terms(query_text: str, limit: int = ZOEKT_MAX_TERMS) -> list[str]:
    """Code-like literals named in the query: file names, backticked spans, and
    identifiers with ``_``, camelCase, or ALL_CAPS, in first-seen order."""
    query_text = _query_values(query_text)
    found = [match.split("/")[-1] for match in _PATH.findall(query_text)]
    # A file name already covers its stem as an identifier.
    stems = {name.rsplit(".", 1)[0].lower() for name in found}
    found += [match.strip() for match in _BACKTICK.findall(query_text)]
    found += [
        token
        for token in _TOKEN.findall(query_text)
        if (_CODEISH.search(token) or (token.isupper() and len(token) >= 3))
        and token.lower() not in stems
    ]
    terms: list[str] = []
    seen: set[str] = set()
    for term in found:
        term = term.strip("`.,():;\"' ")
        if (
            len(term) >= 3
            and not any(char.isspace() for char in term)
            and term.upper() not in _STOP
            and term.lower() not in seen
        ):
            seen.add(term.lower())
            terms.append(term)
    return terms[:limit]


def _zoekt_literal(term: str) -> str:
    """One literal Zoekt query term.

    Zoekt terms are RE2 patterns, and bare words such as ``or``, ``file:x``,
    or ``-name`` are operators. A quoted string is a single pattern whose
    backslashes and quotes are string escapes, so escape for RE2 first.
    """
    pattern = _RE2_META.sub(r"\\\1", term)
    return '"' + pattern.replace("\\", "\\\\").replace('"', '\\"') + '"'


class ZoektCorpusIndex:
    """Zoekt trigram index over every released file and symbol chunk.

    File rows keep their path, so file-name matches work. Symbol rows become
    separate documents mapped back to their file, because released file text
    can be truncated before a symbol. ``rank`` issues one Zoekt query per
    extracted term and orders files by the number of matching terms, then by
    first match. Files that match no term are not ranked.
    """

    def __init__(self, corpus: list[dict[str, Any]], bin_dir: Path):
        self._zoekt = str(bin_dir / "zoekt")
        started = time.perf_counter()
        self._workdir = Path(tempfile.mkdtemp(prefix="codenib-arb-zoekt-"))
        source = self._workdir / "src"
        self._paths = {row["path"] for row in corpus}
        if any(PurePosixPath(path).parts[0] == _SYMBOL_DIR for path in self._paths):
            raise ValueError(f"corpus path collides with {_SYMBOL_DIR}")
        self._documents: dict[str, str] = {}
        for number, row in enumerate(corpus):
            relative = PurePosixPath(row["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"unsafe corpus path: {row['path']}")
            if row["kind"] == "file":
                document = relative.as_posix()
            else:
                document = f"{_SYMBOL_DIR}/{number}.txt"
            self._documents[document] = row["path"]
            target = source / document
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(row["text"], encoding="utf-8")
        index_dir = self._workdir / "index"
        subprocess.run(
            [
                str(bin_dir / "zoekt-index"),
                "-index",
                str(index_dir),
                "-disable_ctags",
                "-file_limit",
                str(64 * 1024 * 1024),
                str(source),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self._index_dir = index_dir
        self.build_seconds = time.perf_counter() - started
        self.size = {
            "documents": len(self._documents),
            "index_bytes": sum(
                path.stat().st_size for path in index_dir.rglob("*") if path.is_file()
            ),
        }
        self.tool_calls = 0

    def _files(self, term: str) -> list[str]:
        process = subprocess.run(
            [
                self._zoekt,
                "-index_dir",
                str(self._index_dir),
                "-l",
                # Terms may start with "-"; never parse them as flags.
                "--",
                _zoekt_literal(term),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if process.returncode not in (0, 1):
            raise RuntimeError(f"zoekt failed: {process.stderr[-2000:]}")
        files = []
        for line in process.stdout.splitlines():
            # Zoekt prints paths relative to the indexed directory.
            path = self._documents.get(line.strip())
            if path is not None:
                files.append(path)
        return list(dict.fromkeys(files))

    def rank(self, query_text: str) -> list[str]:
        terms = zoekt_terms(query_text)
        self.tool_calls = len(terms)
        hits: dict[str, int] = {}
        first: dict[str, int] = {}
        order = 0
        for term in terms:
            for path in self._files(term):
                hits[path] = hits.get(path, 0) + 1
                if path not in first:
                    first[path] = order
                    order += 1
        return sorted(hits, key=lambda path: (-hits[path], first[path]))

    def close(self) -> None:
        shutil.rmtree(self._workdir, ignore_errors=True)


# Full-source code-graph expansion. Scores still use released text only.

GRAPH_SEED_FILES = 5
GRAPH_NEIGHBOR_FILES = 15
GRAPH_BUILD_TIMEOUT = 1800
GRAPH_MIN_LANGUAGE_SHARE = 0.1
_GRAPH_LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".go": "go",
    ".rs": "rust",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".js": "typescript",
    ".jsx": "typescript",
    ".mjs": "typescript",
    ".cjs": "typescript",
    ".java": "java",
}
_SNAPSHOT_RECEIPT = ".codenib-arb-snapshot.json"


def snapshot_languages(corpus: list[dict[str, Any]]) -> list[str]:
    """Graph languages holding at least 10% of the released source files."""
    counts: dict[str, int] = {}
    for row in corpus:
        if row["kind"] == "file":
            language = _GRAPH_LANGUAGES.get(PurePosixPath(row["path"]).suffix)
            if language:
                counts[language] = counts.get(language, 0) + 1
    total = sum(counts.values())
    return sorted(
        (lang for lang, n in counts.items() if n >= GRAPH_MIN_LANGUAGE_SHARE * total),
        key=lambda lang: (-counts[lang], lang),
    )


def _git(checkout: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def fetch_snapshot(
    repo: str, base_commit: str, root: Path, *, remote: str | None = None
) -> Path:
    """A clean working tree at ``base_commit`` without later history.

    A depth-1 fetch of the commit alone keeps the resolving change and every
    later commit out of the object store. A reused checkout must still be
    that single, clean commit.
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not re.fullmatch(
        r"[0-9a-f]{40}", base_commit
    ):
        raise ValueError(f"invalid snapshot identity: {repo}@{base_commit}")
    target = root / repo.replace("/", "__") / base_commit
    if not (target / _SNAPSHOT_RECEIPT).is_file():
        staging = Path(tempfile.mkdtemp(prefix="fetch-", dir=root))
        try:
            _git(staging, "init", "-q")
            url = remote or f"https://github.com/{repo}.git"
            _git(staging, "remote", "add", "origin", url)
            _git(staging, "fetch", "-q", "--depth", "1", "origin", base_commit)
            _git(staging, "checkout", "-q", "--detach", "FETCH_HEAD")
            (staging / ".git" / "info" / "exclude").write_text(
                _SNAPSHOT_RECEIPT + "\n", encoding="utf-8"
            )
            (staging / _SNAPSHOT_RECEIPT).write_text(
                json.dumps({"repo": repo, "base_commit": base_commit}) + "\n",
                encoding="utf-8",
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            staging.rename(target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    if _git(target, "rev-parse", "HEAD") != base_commit:
        raise ValueError(f"checkout is not at {base_commit}")
    if _git(target, "rev-list", "--count", "--all") != "1":
        raise ValueError("checkout has history beyond the base commit")
    if _git(target, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("checkout has local changes")
    return target


def build_graph(checkout: Path, output: Path, languages: list[str]) -> dict[str, Any]:
    """Build a CodeNib graph in a child process, bounded by a timeout."""
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "codenib.eval.benchmarks._arb_methods",
            "build-graph",
            str(checkout),
            str(output),
            ",".join(languages),
        ],
        capture_output=True,
        text=True,
        timeout=GRAPH_BUILD_TIMEOUT,
        check=False,
    )
    report_path = output / "arb_graph_report.json"
    if process.returncode or not report_path.is_file():
        raise RuntimeError(f"graph build failed: {process.stderr[-2000:]}")
    return json.loads(report_path.read_text(encoding="utf-8"))


def _build_graph_main(checkout: str, output: str, languages: str) -> int:
    from codenib.ls_router import build_graph_for_languages_with_report

    result = build_graph_for_languages_with_report(
        checkout,
        output,
        languages=languages.split(","),
        allow_partial=True,
        # Never install dependencies or run project setup commands.
        allow_project_preparation=False,
    )
    report = {
        "requested_languages": list(result.requested_languages),
        "available_languages": list(result.available_languages),
        "failed_languages": dict(result.failed_languages),
        "graph": None,
    }
    if result.graph is not None:
        path = Path(output) / "arb_graph.pkl"
        result.graph.save_graph(str(path))
        report["graph"] = str(path)
    (Path(output) / "arb_graph_report.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    return 0


class GraphExpansionIndex:
    """BM25 seeds expanded over compiler-resolved references (full source).

    The graph comes from SCIP/LSP indexing of a clean checkout at the
    sample's ``base_commit``. ``rank`` keeps the top five BM25 files, then
    adds up to 15 released files whose symbols reference, or are referenced
    by, symbols in those seeds, ordered by connecting references. The
    remaining files follow in BM25 order. Files outside the released
    candidate set are dropped and counted. Without a graph, the ranking is
    BM25.
    """

    tool_calls = 2

    def __init__(
        self,
        corpus: list[dict[str, Any]],
        *,
        repo: str,
        base_commit: str,
        workspace: Path,
    ):
        from codenib.graph.code_graph import CodeGraph

        started = time.perf_counter()
        self._bm25 = BM25CorpusIndex(corpus)
        self._paths = {row["path"] for row in corpus}
        fetched = time.perf_counter()
        checkout = fetch_snapshot(repo, base_commit, workspace)
        self.fetch_seconds = time.perf_counter() - fetched
        built = time.perf_counter()
        languages = snapshot_languages(corpus)
        self._output = Path(tempfile.mkdtemp(prefix="graph-", dir=workspace))
        self._graph = None
        graph_size: dict[str, Any] = {"languages": languages, "available": []}
        try:
            report = build_graph(checkout, self._output, languages) if languages else {}
            graph_size["available"] = report.get("available_languages", [])
            if report.get("failed_languages"):
                graph_size["failed"] = {
                    lang: str(reason)[:300]
                    for lang, reason in report["failed_languages"].items()
                }
            if report.get("graph"):
                self._graph = CodeGraph.load_graph(report["graph"])
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            graph_size["error"] = f"{type(exc).__name__}: {exc}"[:500]
        self.graph_build_seconds = time.perf_counter() - built
        if self._graph is not None:
            graph = self._graph.graph
            graph_size.update(
                vertices=graph.vcount(),
                edges=graph.ecount(),
                reference_edges=sum(
                    edge["type"] == EDGE_TYPE_REFERENCE for edge in graph.es
                ),
            )
        # Fetching source is acquisition, not index construction.
        self.build_seconds = time.perf_counter() - started - self.fetch_seconds
        self.size = {"bm25": self._bm25.size, "graph": graph_size}
        self.graph_used = False
        self.removed_non_candidate_paths = 0

    def _adjacent(self, vertex: int, edge_type: str, mode: str) -> list[int]:
        graph = self._graph.graph
        return [
            graph.es[edge].target if mode == "out" else graph.es[edge].source
            for edge in graph.incident(vertex, mode=mode)
            if graph.es[edge]["type"] == edge_type
        ]

    def _file_symbols(self, path: str) -> list[int]:
        """The file vertex and every symbol it contains, transitively."""
        start = self._graph.name_to_vertex.get(path)
        if start is None:
            return []
        found, pending = [start], [start]
        while pending:
            for child in self._adjacent(pending.pop(), EDGE_TYPE_CONTAIN, "out"):
                if self._graph.graph.vs[child]["type"] != NODE_TYPE_FILE:
                    found.append(child)
                    pending.append(child)
        return found

    def _vertex_file(self, vertex: int) -> str | None:
        attributes = self._graph.graph.vs[vertex].attributes()
        if attributes.get("type") == NODE_TYPE_FILE:
            return attributes.get("name")
        return attributes.get("file")

    def rank(self, query_text: str) -> list[str]:
        ranked = self._bm25.rank(query_text)
        self.graph_used = self._graph is not None
        self.tool_calls = 2 if self.graph_used else 1
        self.removed_non_candidate_paths = 0
        if self._graph is None:
            return ranked
        seeds = ranked[:GRAPH_SEED_FILES]
        weights: dict[str, int] = {}
        first: dict[str, int] = {}
        removed: set[str] = set()
        for seed_rank, seed in enumerate(seeds):
            for symbol in self._file_symbols(seed):
                for neighbor in self._adjacent(
                    symbol, EDGE_TYPE_REFERENCE, "out"
                ) + self._adjacent(symbol, EDGE_TYPE_REFERENCE, "in"):
                    path = self._vertex_file(neighbor)
                    if not path or path in seeds:
                        continue
                    if path not in self._paths:
                        removed.add(path)
                        continue
                    weights[path] = weights.get(path, 0) + 1
                    first.setdefault(path, seed_rank)
        self.removed_non_candidate_paths = len(removed)
        neighbors = sorted(
            weights, key=lambda path: (-weights[path], first[path], path)
        )
        head = seeds + neighbors[:GRAPH_NEIGHBOR_FILES]
        chosen = set(head)
        return complete_file_ranking(
            head + [path for path in ranked if path not in chosen], self._paths
        )

    def close(self) -> None:
        shutil.rmtree(self._output, ignore_errors=True)


if __name__ == "__main__":
    if sys.argv[1:2] != ["build-graph"] or len(sys.argv) != 5:
        raise SystemExit("usage: build-graph CHECKOUT OUTPUT LANGUAGES")
    raise SystemExit(_build_graph_main(*sys.argv[2:]))
