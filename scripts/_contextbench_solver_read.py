#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Capture bounded source reads/searches inside the solver container (stdlib only)."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

MAX_FILE_BYTES = 2 * 1024**2
MAX_LINES = 2000
MAX_SEARCH_FILES = 512
MAX_SEARCH_MATCHES = 200
MAX_SEARCH_OUTPUT_CHARS = 12_000


def _snapshot(root, file):
    path = (root / file).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Read a regular source file inside the checkout")
    relative = path.relative_to(root).as_posix()
    if ".git" in path.relative_to(root).parts:
        raise ValueError("Git internals are not source files")
    # One byte snapshot supplies both the digest and every returned line. A file
    # changing after this read cannot retroactively change its version receipt.
    with path.open("rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES or b"\0" in data:
        raise ValueError("Source must be UTF-8 text no larger than 2 MiB")
    return relative, data, data.decode("utf-8").splitlines()


def _base_digest(root, commit, relative):
    base = subprocess.run(
        ["git", "-C", str(root), "cat-file", "-s", f"{commit}:{relative}"],
        capture_output=True,
        timeout=15,
    )
    base_sha = None
    if base.returncode == 0 and int(base.stdout) <= MAX_FILE_BYTES:
        base_data = subprocess.check_output(
            ["git", "-C", str(root), "cat-file", "blob", f"{commit}:{relative}"],
            timeout=15,
        )
        base_sha = hashlib.sha256(base_data).hexdigest()
    return base_sha


def _version(root, commit, relative, data, lines):
    return {
        "file": relative,
        "base_commit": commit,
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "base_sha256": _base_digest(root, commit, relative),
        "source_line_count": len(lines),
    }


def read_source(root, commit, file, start, end):
    root = Path(root).resolve(strict=True)
    if not 1 <= start <= end or end - start + 1 > MAX_LINES:
        raise ValueError(f"Use 1-based inclusive ranges of at most {MAX_LINES} lines")
    relative, data, lines = _snapshot(root, file)
    if start > len(lines):
        raise ValueError("Start line is outside the file")
    return {
        **_version(root, commit, relative, data, lines),
        "start_line": start,
        "lines": lines[start - 1 : end],
    }


def search_line(file, number, text):
    """Every complete visible record carries its own unambiguous file identity."""
    return json.dumps({"file": file, "line": number, "text": text}, ensure_ascii=False)


def search_source(root, commit, pattern, path, limit=100):
    """Search current visible files; each matching file has its own byte snapshot.

    This is not a simultaneous repository snapshot. Only matches from a file
    whose captured bytes equal its pinned base blob can earn base-line credit.
    Tests are included. Caps/skipped files are explicit; shell timeout ownership
    stays with the existing solver environment.
    """
    if not isinstance(pattern, str) or not 1 <= len(pattern) <= 512:
        raise ValueError("Use a nonempty Python regex of at most 512 characters")
    if type(limit) is not int or not 1 <= limit <= MAX_SEARCH_MATCHES:
        raise ValueError(f"Use a match limit from 1 to {MAX_SEARCH_MATCHES}")
    regex = re.compile(pattern)
    root = Path(root).resolve(strict=True)
    target = (root / path).resolve(strict=True)
    if not target.is_relative_to(root) or ".git" in target.relative_to(root).parts:
        raise ValueError("Search source inside the checkout, outside Git internals")
    relative = target.relative_to(root).as_posix()
    raw = subprocess.check_output(
        [
            "git",
            "-C",
            str(root),
            "--literal-pathspecs",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            relative,
        ],
        timeout=15,
    )
    files = sorted(set(raw.decode("utf-8").split("\0")) - {""})
    sources, seen = [], set()
    count = used = scanned = skipped = 0
    limits = []
    for file in files:
        if scanned == MAX_SEARCH_FILES:
            limits.append("file_scan_limit")
            break
        scanned += 1
        try:
            name, data, lines = _snapshot(root, file)
        except (OSError, ValueError):
            skipped += 1
            continue
        if name in seen:
            continue
        seen.add(name)
        matches = []
        for number, text in enumerate(lines, 1):
            if not regex.search(text):
                continue
            size = len(search_line(name, number, text)) + 1
            if used + size > MAX_SEARCH_OUTPUT_CHARS:
                limits.append("output_limit")
                break
            matches.append({"line": number, "text": text})
            used += size
            count += 1
            if count == limit:
                limits.append("match_limit")
                break
        if matches:
            sources.append(
                {**_version(root, commit, name, data, lines), "matches": matches}
            )
        if limits:
            break
    return {
        "sources": sources,
        "files_considered": len(files),
        "files_scanned": scanned,
        "skipped_files": skipped,
        "matches_returned": count,
        "limits_reached": limits,
    }


if __name__ == "__main__":
    try:
        request = json.loads(sys.argv[1])
        operation = request.pop("operation", "read")
        if operation == "read":
            result = {"source": read_source(**request)}
        elif operation == "search":
            result = {"search": search_source(**request)}
        else:
            raise ValueError("Unknown source operation")
    except (
        OSError,
        ValueError,
        re.error,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        result = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, ensure_ascii=False))
