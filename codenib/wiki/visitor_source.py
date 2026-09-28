# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Bounded public GitHub source for a visitor Wiki, never an executable checkout."""

from __future__ import annotations

import io
import json
import re
import stat
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from urllib.parse import quote

from ..code_chunker import CodeChunker, RepoChunkingConfig
from ..compiler.manifest import RepoManifest
from ..languages import extension_to_language_map
from ..source_fingerprint import capture_repository_source
from .visitor_provider import WikiRunStopped

REPOSITORY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}")
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_SOURCE_BYTES = 40 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_FILES = 4000


def github_bytes(url: str, limit: int, check) -> bytes:
    import requests

    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.get(
                url,
                headers={"Accept": "application/vnd.github+json"},
                timeout=(10, 30),
                allow_redirects=False,
                stream=True,
            ) as response:
                if response.status_code != 200:
                    raise WikiRunStopped(
                        f"GitHub returned HTTP {response.status_code}. "
                        "Use the current URL of a public repository, or retry later."
                    )
                data = bytearray()
                for chunk in response.iter_content(65536):
                    check()
                    data.extend(chunk)
                    if len(data) > limit:
                        raise WikiRunStopped(
                            "This repository exceeds the hosted Wiki size limit."
                        )
                return bytes(data)
    except requests.RequestException:
        raise WikiRunStopped(
            "GitHub download failed. Completed pages are saved."
        ) from None


def repository_revision(repository: str, check) -> str:
    if not REPOSITORY_RE.fullmatch(repository) or repository.split("/")[1] in {
        ".",
        "..",
    }:
        raise WikiRunStopped("Enter a public GitHub repository as owner/repo.")
    try:
        metadata = json.loads(
            github_bytes(
                f"https://api.github.com/repos/{repository}", 128 * 1024, check
            )
        )
        if metadata.get("private") is not False:
            raise WikiRunStopped("Only public GitHub repositories are supported.")
        branch = quote(metadata["default_branch"], safe="")
        data = json.loads(
            github_bytes(
                f"https://api.github.com/repos/{repository}/commits/{branch}",
                2 * 1024 * 1024,
                check,
            )
        )
        commit = data["sha"]
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError
        return commit
    except (ValueError, KeyError, TypeError, AttributeError):
        raise WikiRunStopped(
            "GitHub did not return a valid public repository revision."
        ) from None


def extract_source(payload: bytes, root: Path, check) -> list[dict]:
    """Validate every path; skip oversized files without decompressing them.

    The expanded-byte budget bounds retained source. The compressed archive and
    member-count limits still bound the full input, including skipped members.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES * 2:
                raise WikiRunStopped(
                    "This repository has too many files for hosted generation."
                )
            files, skipped, total, seen, prefix = [], [], 0, set(), None
            for item in entries:
                check()
                path = PurePosixPath(item.filename)
                parts = item.filename.rstrip("/").split("/")
                mode = item.external_attr >> 16
                if (
                    not parts
                    or path.is_absolute()
                    or any(part in {"", ".", "..", ".git"} for part in parts)
                    or any(ord(c) < 32 for c in item.filename)
                    or "\\" in item.filename
                    or ":" in item.filename
                    or len(item.filename) > 512
                    or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR})
                    or (prefix is not None and parts[0] != prefix)
                ):
                    raise WikiRunStopped(
                        "The repository archive contains an unsupported path or link."
                    )
                prefix = parts[0]
                if item.is_dir():
                    continue
                if len(parts) < 2:
                    raise WikiRunStopped(
                        "The repository archive contains an unsupported path."
                    )
                relative = "/".join(parts[1:])
                if relative.casefold() in seen:
                    raise WikiRunStopped(
                        "The repository archive contains duplicate paths."
                    )
                seen.add(relative.casefold())
                if len(seen) > MAX_FILES:
                    raise WikiRunStopped(
                        "This repository has too many files for hosted generation."
                    )
                if item.file_size > MAX_FILE_BYTES:
                    skipped.append({"path": relative, "size_bytes": item.file_size})
                    continue
                total += item.file_size
                files.append((item, relative))
                if total > MAX_SOURCE_BYTES:
                    raise WikiRunStopped(
                        "This repository exceeds the hosted Wiki size limit."
                    )
            for item, relative in files:
                check()
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                data = archive.read(item)
                if len(data) != item.file_size:
                    raise WikiRunStopped("The repository archive failed validation.")
                target.write_bytes(data)
            return skipped
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError):
        raise WikiRunStopped(
            "The repository archive could not be read safely."
        ) from None


@contextmanager
def visitor_source(repository: str, commit: str, attempt_id: str, check, progress):
    # Temporary source is owned by this run and removed on every exit. Resumes
    # download the same immutable commit; only Wiki outputs survive the call.
    progress("downloading", "")
    payload = github_bytes(
        f"https://codeload.github.com/{repository}/zip/{commit}",
        MAX_ARCHIVE_BYTES,
        check,
    )
    with tempfile.TemporaryDirectory(prefix="codenib-visitor-wiki-") as directory:
        root = Path(directory)
        skipped_files = extract_source(payload, root, check)
        progress("analyzing", "")
        with capture_repository_source(root, check_cancelled=check) as binding:
            extensions = extension_to_language_map("chunker")
            languages = sorted(
                {
                    extensions[Path(p).suffix]
                    for p in binding.borrow_reader().file_paths
                    if Path(p).suffix in extensions
                }
            )
            if not languages:
                raise WikiRunStopped(
                    "No supported source files remain after files larger than "
                    "4 MiB are skipped. Run CodeNib locally for larger files."
                    if skipped_files
                    else "No supported source language was found in this repository."
                )
            chunks = CodeChunker(
                language=languages[0],
                chunk_depth=2,
                max_lines_per_chunk=100,
                repo_config=RepoChunkingConfig(
                    languages=languages, filter_tests=False, max_file_size_mb=4
                ),
            ).chunk_repository_source(binding, check_cancelled=check)
            if len(chunks) > 50000:
                raise WikiRunStopped(
                    "This repository has too many symbols for hosted generation."
                )
            documents = [
                SimpleNamespace(
                    page_content=chunk.content,
                    metadata={
                        "file": chunk.file,
                        "name": chunk.name,
                        "chunk_type": chunk.chunk_type,
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                    },
                )
                for chunk in chunks
            ]
            identity = binding.authenticated_identity_snapshot(check_cancelled=check)
            bundle = SimpleNamespace(
                entry=SimpleNamespace(
                    repo=repository,
                    instance_id=attempt_id,
                    repo_dir=str(root),
                    base_commit=commit,
                    commit_short=commit[:8],
                    language=languages[0],
                ),
                manifest=RepoManifest(
                    repo_path=str(root),
                    commit=commit,
                    source_fingerprint=identity.fingerprint,
                    languages=languages,
                    file_count=len(identity.file_records),
                ),
                vector_store=None,
                bm25=None,
                wiki_documents=documents,
                skipped_files=skipped_files,
                source_reader=binding.borrow_reader(),
                code_graph=lambda: None,
            )
            from .visitor_graph import build_visitor_graph

            build_visitor_graph(bundle, check, progress)
            yield bundle, binding
