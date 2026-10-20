# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded document context for reader-facing repository visuals."""

import re
from contextlib import nullcontext
from pathlib import PurePosixPath

_SOURCE_PATH = re.compile(
    r"[\w.-]+(?:/[\w.-]+)+\.(?:py|tsx?|jsx?|go|rs|cpp|cc|h|hpp|java|cs|rb|php)\b"
)


def visual_document_contexts(persisted, facts, bundle):
    """Use the pinned reader; never reopen arbitrary manifest paths on disk.

    At most eight documents and four references per visual are inspected.
    The source owner remains retained by the caller's generation pin; the
    existing read session validates the batch before its result is published.
    """
    artifacts = {
        item["path"]: item for item in persisted["media_manifest"].get("artifacts", [])
    }
    reader = getattr(bundle, "source_reader", None)
    session = getattr(bundle, "source_read_session", None)
    documents = {}
    contexts = {}
    with session() if callable(session) else nullcontext():
        for fact in facts:
            artifact = artifacts.get(fact["artifact_path"], {})
            caption = str(artifact.get("caption", ""))[:240]
            references = []
            text_parts = [caption]
            for ref in artifact.get("references", [])[:4]:
                path = ref.get("markdown_path", "")
                line = ref.get("line", 0)
                if (
                    reader is None
                    or not isinstance(path, str)
                    or PurePosixPath(path).suffix.lower()
                    not in {".md", ".mdx", ".markdown"}
                    or type(line) is not int
                    or line < 1
                    or reader.captured_relative_path(path) != path
                ):
                    continue
                if path not in documents:
                    if len(documents) >= 8:
                        continue
                    documents[path] = None
                    payload = reader.read_prefix(path, max_bytes=128 * 1024 + 1)
                    # Do not interpret a truncated final line as a document.
                    if len(payload) > 128 * 1024:
                        continue
                    documents[path] = payload.decode(
                        "utf-8", errors="replace"
                    ).splitlines()
                lines = documents[path]
                if lines is None or line > len(lines):
                    continue
                headings = []
                fenced = False
                for value in lines[:line]:
                    if value.lstrip().startswith(("```", "~~~")):
                        fenced = not fenced
                    match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", value)
                    if match and not fenced:
                        level = len(match[1])
                        headings = [item for item in headings if item[0] < level]
                        headings.append((level, match[2][:160]))
                excerpt = "\n".join(lines[max(0, line - 7) : line + 6])[:1200]
                title = str(ref.get("alt_text") or ref.get("title") or caption)[:240]
                section = " / ".join(item[1] for item in headings)[-320:]
                references.append(
                    {
                        "file": path,
                        "line": line,
                        "title": title,
                        "section": section,
                        "excerpt": excerpt,
                    }
                )
                text_parts.extend((title, section, excerpt))
            # Only explicit repository paths that the pinned reader recognizes
            # contribute a source relationship. Heuristic binding scores do not.
            text_parts.extend(
                str(entity.get("name", "")) for entity in fact["entities"]
            )
            source_paths = []
            if reader is not None:
                for path in dict.fromkeys(_SOURCE_PATH.findall("\n".join(text_parts))):
                    if reader.captured_relative_path(path) == path:
                        source_paths.append(path)
                    if len(source_paths) == 8:
                        break
            contexts[fact["artifact_path"]] = {
                "caption": caption,
                "references": references,
                "source_paths": source_paths,
            }
    return contexts
