# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Extract a concise project purpose from repository README files."""

from __future__ import annotations

import os
import re

README_NAMES = ("README.md", "README.rst", "README.txt", "README", "readme.md")

_README_SKIP = re.compile(
    r"\b(install|download|getting started|to get started|usage|build from source"
    r"|clone|npm i\b|pip install|cargo add|see (the )?docs|documentation"
    r"|these steps|version information|for example|e\.g\.)\b",
    re.IGNORECASE,
)
_README_SKIP_PREFIX = (
    "all detailed ",
    "note:",
    "open ",
    "run ",
    "see ",
    "this command ",
    "this example ",
    "tip:",
    "warning:",
    "you can choose ",
    "$ ",
)
_README_SECTION_TITLES = {
    "acknowledgements",
    "building",
    "changelog",
    "configuration",
    "contributing",
    "development",
    "documentation",
    "examples",
    "features",
    "getting started",
    "installation",
    "license",
    "prerequisites",
    "quick start",
    "quickstart",
    "requirements",
    "run",
    "setup",
    "usage",
}


def _plain_text(line: str) -> str:
    line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
    line = re.sub(r"[*_`]", "", line)
    return re.sub(r"<[^>]+>", "", line).strip()


def _descriptive_heading(line: str) -> str:
    heading = _plain_text(line.lstrip("#").strip())
    normalized = re.sub(r"[^a-z0-9]+", " ", heading.lower()).strip()
    if (
        len(heading.split()) >= 4
        and normalized not in _README_SECTION_TITLES
        and not _README_SKIP.search(heading)
    ):
        return heading
    return ""


def readme_summary(text: str, limit: int = 160) -> str:
    """Return the first descriptive sentence, excluding README boilerplate."""

    in_fence = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith("#"):
            if re.match(r"^#(?!#)\s+", line):
                heading = _descriptive_heading(line)
                if heading:
                    return heading[:limit] + "…" if len(heading) > limit else heading
            continue
        if line.startswith((">", "<", "---", "===", "|", "- ", "* ")):
            continue
        if line.startswith(("![", "[![")) or line.startswith("["):
            continue
        line = _plain_text(line)
        if line.endswith(":") or len(line.split()) < 6 or _README_SKIP.search(line):
            continue
        prefix_text = re.sub(r"^[^a-z0-9$]+", "", line.lower())
        if prefix_text.startswith(_README_SKIP_PREFIX):
            continue
        return (line[:limit] + "…") if len(line) > limit else line
    return ""


def read_repository_summary(repo_dir: str, limit: int = 160) -> str:
    """Read a repository's conventional README and extract its purpose."""

    root = os.path.abspath(repo_dir)
    for name in README_NAMES:
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                return readme_summary(handle.read(), limit=limit)
        except OSError:
            return ""
    return ""
