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
import os
import re
import shutil
import signal
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
# Immutable Hugging Face commit; a mutable default branch could change weights.
DENSE_REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
DENSE_MAX_SEQ_LENGTH = 512
DENSE_BATCH_SIZE = 64
HYBRID_RRF_K = 60
RERANK_MODEL = "Qwen/Qwen3-Reranker-4B"
RERANK_REVISION = "22e683669bc0f0bd69640a1354a6d0aebcfeede5"
RERANK_TOP_K = 50
RERANK_MAX_LENGTH = 8192
RERANK_BATCH_SIZE = 4
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
                # Keep encoder output precision so near ties rank as encoded.
                self._vectors[key] = np.asarray(vector, dtype=np.float32)
        self.encoded_documents += len(missing)
        self.reused_documents += len(texts) - len(missing)
        return [self._vectors[key] for key in keys]

    def embed_query(self, text: str) -> list[float]:
        return self._embedding.embed_query(text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._embedding, name)


def dense_precision(device: str | None = None) -> tuple[str, str]:
    """(device, dtype) a dense run uses; part of its resume receipt."""
    import torch

    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    if device.startswith("cuda"):
        # is_bf16_supported() checks the current device, so select ours first.
        with torch.cuda.device(torch.device(device)):
            if torch.cuda.is_bf16_supported():
                return device, "bfloat16"
    return device, "float32"


class DenseEncoder:
    """One embedding model, loaded once and shared by every snapshot index."""

    def __init__(self, config: dict[str, Any]):
        import torch

        from codenib.index.embedding import CodeVectorStore

        started = time.perf_counter()
        self.store_options = {
            "embedding_model": config["model"],
            "embedding_provider": "huggingface",
            "revision": config["revision"],
            # Probed from the model, so any embedding size works.
            "dimension": None,
            "index_type": "flat",
            "index_metric": "ip",
        }
        # A prompt name, if the run selects one, resolves in the pinned model.
        prompt_name = config.get("query_prompt_name")
        template = CodeVectorStore(
            **self.store_options,
            device=config["device"],
            default_batch_size=config["batch_size"],
            max_seq_length=config["max_seq_length"],
            # No document instruction, as in ARB's embedding baseline; the
            # query instruction is empty unless a run selects one.
            query_prompt=None if prompt_name else config.get("query_prompt", ""),
            query_prompt_name=prompt_name,
            document_prompt="",
            encode_kwargs={"normalize_embeddings": True},
        )
        if config["dtype"] == "bfloat16":
            template.embedding.client.to(torch.bfloat16)
        self.embedding = _CachedEmbedding(template.embedding)
        self.load_seconds = time.perf_counter() - started


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


class Reranker:
    """One cross-encoder, loaded once and shared by every snapshot index."""

    def __init__(self, config: dict[str, Any]):
        from codenib.index.rerank.cross_encoder import build_reranker

        started = time.perf_counter()
        self.model = build_reranker(
            config["model"],
            revision=config["revision"],
            max_length=config["max_length"],
            batch_size=config["batch_size"],
            device=config["device"],
            torch_dtype=config["dtype"],
        )
        self.load_seconds = time.perf_counter() - started


class RerankCorpusIndex:
    """Reorders a base ranking's top files with a cross-encoder.

    Each file is scored as its path and released text, which the model
    truncates to its length limit. The scored order is fused with the base
    order by file-level RRF, and files below ``top_k`` keep the base order.
    The result holds exactly the base ranking's files.
    """

    tool_calls = 1

    def __init__(self, corpus: list[dict[str, Any]], reranker: Reranker, top_k: int):
        started = time.perf_counter()
        self._paths = {row["path"] for row in corpus}
        self._texts = {
            row["path"]: row.get("text") or ""
            for row in corpus
            if row.get("kind") == "file"
        }
        self._reranker = reranker
        self._top_k = top_k
        self.build_seconds = time.perf_counter() - started
        self.size = {"files": len(self._paths), "top_k": top_k}

    def rerank(self, query_text: str, base: list[str]) -> list[str]:
        from codenib.ops.retrieve import merge_file_rankings
        from codenib.types import QueriedNode

        # The evaluator keeps a path's first occurrence; so does the head.
        ranking = list(dict.fromkeys(base))
        head, tail = ranking[: self._top_k], ranking[self._top_k :]
        model = self._reranker.model
        before = getattr(model, "oom_fallbacks", 0)
        scores = model.score(
            query_text, [f"{path}\n{self._texts.get(path, '')}" for path in head]
        )
        # A file the model could not score is a failed case, not a zero score.
        unscored = getattr(model, "oom_fallbacks", 0) - before
        if unscored:
            raise RuntimeError(f"the reranker ran out of memory on {unscored} files")
        order = [
            head[i] for i in sorted(range(len(head)), key=lambda i: (-scores[i], i))
        ]
        branches = [
            [
                QueriedNode(node_name=path, type=NODE_TYPE_FILE, file=path)
                for path in ranking
            ]
            for ranking in (head, order)
        ]
        fused = merge_file_rankings(branches, rrf_k=HYBRID_RRF_K)
        reranked = [*(node.file for node in fused), *tail]
        # Exactly the base's files, which may be fewer than the corpus.
        if sorted(reranked) != sorted(ranking):
            raise ValueError("reranking changed the base ranking's files")
        return reranked


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
_SNAPSHOT_RECEIPT = ".codenib-arb-snapshot.json"


def snapshot_languages(corpus: list[dict[str, Any]]) -> list[str]:
    """Graph backends holding at least 10% of the released source files.

    Extensions route through CodeNib's language registry, so every language
    with a graph backend is eligible.
    """
    from codenib.languages import extension_to_language_map

    routes = extension_to_language_map("graph")
    counts: dict[str, int] = {}
    for row in corpus:
        if row["kind"] == "file":
            language = routes.get(PurePosixPath(row["path"]).suffix.lower())
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


def snapshot_path(repo: str, base_commit: str, root: Path) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not re.fullmatch(
        r"[0-9a-f]{40}", base_commit
    ):
        raise ValueError(f"invalid snapshot identity: {repo}@{base_commit}")
    return root / repo.replace("/", "__") / base_commit


def snapshot_lock(checkout: Path) -> Any:
    from filelock import FileLock

    return FileLock(str(checkout) + ".lock")


def is_snapshot_checkout(path: Path) -> bool:
    """Whether ``path`` is a checkout that ``fetch_snapshot`` published."""
    return (path / ".git" / _SNAPSHOT_RECEIPT).is_file()


def fetch_snapshot(
    repo: str, base_commit: str, root: Path, *, remote: str | None = None
) -> Path:
    """A clean working tree at ``base_commit`` without later history.

    The caller must hold ``snapshot_lock`` for the returned path while it
    validates, cleans, and indexes the checkout. A depth-1 fetch of the commit
    alone keeps the resolving change and every later commit out of the object
    store. Every use revalidates that single commit and removes untracked and
    ignored files, including earlier indexer output.
    """
    target = snapshot_path(repo, base_commit, root)
    receipt = target / ".git" / _SNAPSHOT_RECEIPT
    if not receipt.is_file():
        # Only a missing or empty target is replaced. A directory without the
        # receipt, even a Git checkout, was not published here.
        if target.is_dir() and any(target.iterdir()):
            raise ValueError(
                f"{target} is not a snapshot checkout published here; "
                "remove it to fetch again"
            )
        shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="fetch-", dir=target.parent))
        try:
            _git(staging, "init", "-q")
            url = remote or f"https://github.com/{repo}.git"
            _git(staging, "remote", "add", "origin", url)
            _git(staging, "fetch", "-q", "--depth", "1", "origin", base_commit)
            _git(staging, "checkout", "-q", "--detach", "FETCH_HEAD")
            (staging / ".git" / _SNAPSHOT_RECEIPT).write_text(
                json.dumps({"repo": repo, "base_commit": base_commit}) + "\n",
                encoding="utf-8",
            )
            # The receipt is written last; a crash leaves no published checkout.
            staging.rename(target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    if _git(target, "rev-parse", "HEAD") != base_commit:
        raise ValueError(f"checkout is not at {base_commit}")
    if _git(target, "rev-list", "--count", "--all") != "1":
        raise ValueError("checkout has history beyond the base commit")
    # assume-unchanged or skip-worktree would hide edited tracked files.
    if any(
        not line.startswith("H ")
        for line in _git(target, "ls-files", "-v").splitlines()
    ):
        raise ValueError("checkout has hidden Git index flags")
    _git(target, "reset", "-q", "--hard", "HEAD")
    _git(target, "clean", "-ffdxq")
    if _git(target, "status", "--porcelain", "--untracked-files=all", "--ignored"):
        raise ValueError("checkout is not clean after reset")
    return target


def build_graph(checkout: Path, output: Path, languages: list[str]) -> dict[str, Any]:
    """Build a CodeNib graph in a child process group, bounded by a timeout.

    Indexers spawn compilers and build tools, so a timeout kills the whole
    group rather than only the immediate child.
    """
    returncode, stderr = run_process_group(
        [
            sys.executable,
            "-m",
            "codenib.eval.benchmarks._arb_methods",
            "build-graph",
            str(checkout),
            str(output),
            ",".join(languages),
        ],
        GRAPH_BUILD_TIMEOUT,
    )
    report_path = output / "arb_graph_report.json"
    if returncode or not report_path.is_file():
        raise RuntimeError(f"graph build failed: {stderr[-2000:]}")
    return json.loads(report_path.read_text(encoding="utf-8"))


def run_process_group(command: list[str], timeout: float) -> tuple[int, str]:
    """Run ``command`` in a new session; on timeout kill its whole group."""
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    return process.returncode, stderr


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
        started = time.perf_counter()
        self._bm25 = BM25CorpusIndex(corpus)
        self._paths = {row["path"] for row in corpus}
        languages = snapshot_languages(corpus)
        self._output = Path(tempfile.mkdtemp(prefix="graph-", dir=workspace))
        self._graph = None
        graph_size: dict[str, Any] = {"languages": languages, "available": []}
        # Lock order: the run's output lock, then this snapshot's lock. The
        # holder owns the checkout's working tree until the graph is built.
        with snapshot_lock(snapshot_path(repo, base_commit, workspace)):
            fetched = time.perf_counter()
            checkout = fetch_snapshot(repo, base_commit, workspace)
            self.fetch_seconds = time.perf_counter() - fetched
            self._build(checkout, languages, graph_size)
        self.graph_build_seconds = (
            time.perf_counter()
            - started
            - self.fetch_seconds
            - self._bm25.build_seconds
        )
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
        graph_size["build_seconds"] = self.graph_build_seconds
        self.size = {"bm25": self._bm25.size, "graph": graph_size}
        self.graph_used = False
        self.removed_non_candidate_paths = 0

    def _build(
        self, checkout: Path, languages: list[str], graph_size: dict[str, Any]
    ) -> None:
        from codenib.graph.code_graph import CodeGraph

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
