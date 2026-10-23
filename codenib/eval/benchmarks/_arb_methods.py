# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Dense, hybrid, and Zoekt ARB rankers over the released corpus (labs).

Each index is built once per corpus snapshot and exposes ``build_seconds``,
``size``, ``tool_calls`` (searches issued by the latest ``rank``), and
``rank(query_text)``. Like BM25, they read only the released query text and
corpus rows; gold, fix commits, and annotations never reach them.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

import numpy as np

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
    # Bare Zoekt terms are RE2 patterns; escape them to match literally.
    return _RE2_META.sub(r"\\\1", term)


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
