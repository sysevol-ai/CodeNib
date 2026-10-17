#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Read one bounded source version inside the solver container (stdlib only)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

MAX_FILE_BYTES = 2 * 1024**2
MAX_LINES = 2000


def read_source(root, commit, file, start, end):
    root = Path(root).resolve(strict=True)
    path = (root / file).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Read a regular source file inside the checkout")
    relative = path.relative_to(root).as_posix()
    if ".git" in path.relative_to(root).parts:
        raise ValueError("Git internals are not source files")
    if not 1 <= start <= end or end - start + 1 > MAX_LINES:
        raise ValueError(f"Use 1-based inclusive ranges of at most {MAX_LINES} lines")
    # One byte snapshot supplies both the digest and every returned line. A file
    # changing after this read cannot retroactively change its version receipt.
    with path.open("rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES or b"\0" in data:
        raise ValueError("Source must be UTF-8 text no larger than 2 MiB")
    lines = data.decode("utf-8").splitlines()
    if start > len(lines):
        raise ValueError("Start line is outside the file")
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
    return {
        "file": relative,
        "base_commit": commit,
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "base_sha256": base_sha,
        "source_line_count": len(lines),
        "start_line": start,
        "lines": lines[start - 1 : end],
    }


if __name__ == "__main__":
    try:
        result = {"source": read_source(**json.loads(sys.argv[1]))}
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        result = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, ensure_ascii=False))
