# SPDX-FileCopyrightText: 2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Render ready, source-checked Wiki pages as portable OKF v0.2 documents.

This is an output format, not another Wiki store. The caller authenticates the
source snapshot and publishes the returned files through its existing boundary.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, quote, urlsplit

import yaml

from ..languages import extension_to_language_map
from .fences import _fence_spans, fence_marker, strip_code_fences
from .lead import overview_lead

OKF_VERSION = "0.2"
OKF_EXPORT_MANIFEST = "codenib-okf.json"
_PAGE_ID = re.compile(r"[A-Za-z0-9_.-]+")
_LINK = re.compile(r"(?P<start>!?\[(?P<label>[^\]\n]*)\]\()(?P<url>[^\s)]+)(?P<end>\))")
_CITATION = re.compile(r"\[(?P<id>[ER]\d+)\](?![\[(])")
_INLINE_CODE = re.compile(r"(?<!`)(?P<fence>`+)(?!`)[\s\S]*?(?<!`)(?P=fence)(?!`)")


def _json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    ).encode()


def _document(metadata: Mapping[str, Any], body: str) -> bytes:
    frontmatter = yaml.safe_dump(dict(metadata), allow_unicode=True, sort_keys=False)
    return f"---\n{frontmatter}---\n\n{body.rstrip()}\n".encode("utf-8")


def _label(value: object) -> str:
    return re.sub(r"([\\\[\]])", r"\\\1", " ".join(str(value).split()))


def _relative(target: str, current: str) -> str:
    return posixpath.relpath(target, posixpath.dirname(current) or ".")


def _prose_transform(markdown: str, transform: Any) -> str:
    """Rewrite prose links/markers while leaving fenced and inline code exact."""

    lines = markdown.splitlines(keepends=True)
    result, cursor = [], 0
    for start, end in _fence_spans([line.rstrip("\r\n") for line in lines]):
        result.append(transform("".join(lines[cursor:start])))
        result.extend(lines[start:end])
        cursor = end
    result.append(transform("".join(lines[cursor:])))
    return "".join(result)


def _rewrite_markdown(
    markdown: str,
    path: str,
    paths: Mapping[str, str],
    markers: Mapping[str, tuple[str, str]],
) -> str:
    def footnote(marker: str) -> str:
        if marker not in markers:
            raise ValueError(f"OKF citation has no captured evidence: {marker}")
        return f"[^{markers[marker][0]}]"

    def rewrite(text: str) -> str:
        code_spans = [match.span() for match in _INLINE_CODE.finditer(text)]

        def link(match: re.Match[str]) -> str:
            if any(start <= match.start() < end for start, end in code_spans):
                return match[0]
            # AgentWiki saves linked ledger markers, rather than bare [E1].
            # Their live-reader anchors do not exist in a Markdown export.
            marker = match["label"]
            if (
                re.fullmatch(r"[ER]\d+", marker)
                and match["start"] == f"[{marker}]("
                and match["url"] == f"#evidence-{marker}"
            ):
                return footnote(marker)
            url = urlsplit(match["url"])
            if url.scheme or url.netloc or not url.query:
                return match[0]
            query = parse_qs(url.query)
            if "p" not in query:
                return match[0]
            target = query["p"][0]
            if target not in paths:
                raise ValueError(f"OKF page link names an unavailable page: {target}")
            destination = _relative(paths[target], path)
            if url.fragment:
                destination += "#" + url.fragment
            return match["start"] + destination + match["end"]

        def citation(match: re.Match[str]) -> str:
            return footnote(match["id"])

        text = _LINK.sub(link, text)
        # Code examples containing [E1] or a page query are examples, not
        # prose references. Fenced blocks are already skipped by the caller.
        result, cursor = [], 0
        for match in _INLINE_CODE.finditer(text):
            result.extend(
                (_CITATION.sub(citation, text[cursor : match.start()]), match[0])
            )
            cursor = match.end()
        result.append(_CITATION.sub(citation, text[cursor:]))
        return "".join(result)

    return _prose_transform(markdown, rewrite).rstrip()


def render_okf_bundle(
    tree: Sequence[Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
    *,
    repository: str,
    commit: str,
    source_fingerprint: str,
    snapshot_at: str,
) -> dict[str, bytes]:
    """Return deterministic document bytes for one captured Wiki generation.

    The index snapshot's date is used for the snapshot log, never as the content's
    generation/verification time. Source excerpts are provided by the caller's
    authenticated reader; cached snippets must already have been replaced.
    """

    paths: dict[str, str] = {}
    summaries: dict[str, str] = {}
    directories: dict[str, list[tuple[str, str, str]]] = {"": []}
    page_by_id = {str(page["id"]): page for page in pages}
    if len(page_by_id) != len(pages):
        raise ValueError("OKF pages contain duplicate ids")

    def visit(nodes: Sequence[Mapping[str, Any]], directory: str = "") -> None:
        for node in nodes:
            page_id = str(node.get("id") or "")
            if not _PAGE_ID.fullmatch(page_id) or page_id in {".", ".."}:
                raise ValueError("OKF page id must be a safe path component")
            if page_id in paths or page_id not in page_by_id:
                raise ValueError("OKF outline must name each ready page exactly once")
            page = page_by_id[page_id]
            path = posixpath.join(directory, f"page-{page_id}.md")
            paths[page_id] = path
            summary = overview_lead(strip_code_fences(str(page.get("markdown") or "")))
            summaries[page_id] = " ".join(
                (summary or str(page.get("title") or page_id)).split()
            )
            directories[directory].append(
                (path, str(page.get("title") or page_id), summaries[page_id])
            )
            if node.get("children"):
                child_dir = posixpath.join(directory, f"section-{page_id}")
                directories[child_dir] = []
                directories[directory].append(
                    (
                        f"{child_dir}/index.md",
                        str(page.get("title") or page_id) + " chapters",
                        "Related chapters",
                    )
                )
                visit(node["children"], child_dir)

    visit(tree)
    if set(paths) != set(page_by_id):
        raise ValueError("OKF outline does not cover every page")

    files: dict[str, bytes] = {}
    references: dict[str, tuple[str, str]] = {}
    for page_id, path in paths.items():
        page = page_by_id[page_id]
        evidence = dict(page.get("evidence") or {})
        evidence_path = f"evidence/{page_id}.json"
        files[evidence_path] = _json(
            {
                "page_id": page_id,
                "repository": repository,
                "commit": commit,
                "source_fingerprint": source_fingerprint,
                "line_base": 1,
                "end_line": "inclusive",
                "evidence": evidence,
                "citations": page.get("citations") or [],
                "generation": page.get("generation") or {},
                "grounding": page.get("grounding") or {},
                "quality": page.get("quality") or {},
            }
        )
        sources: list[dict[str, str]] = []
        markers: dict[str, tuple[str, str]] = {}
        items = evidence.get("items") or []
        for index, citation in enumerate(page.get("citations") or [], 1):
            source_file = str(citation.get("file") or "")
            if not source_file:
                raise ValueError("OKF citation has no source file")
            start, end = citation.get("start_line"), citation.get("end_line")
            identity = [repository, source_fingerprint, source_file, start, end]
            source_id = "source-" + hashlib.sha256(_json(identity)).hexdigest()[:24]
            reference = f"references/{source_id}.md"
            title = source_file + (f":{start}-{end}" if start is not None else "")
            source_meta = {
                "type": "Source Reference",
                "title": title,
                "description": f"Captured source evidence from {source_file}.",
                "resource": "repo://"
                + quote(source_file, safe="/")
                + (
                    f"#L{start}-L{end}" if start is not None and end is not None else ""
                ),
                "x_codenib": {
                    "repository": repository,
                    "commit": commit,
                    "source_fingerprint": source_fingerprint,
                    "file": source_file,
                    "start_line": start,
                    "end_line": end,
                    "line_base": 1,
                    "end_line_convention": "inclusive",
                },
            }
            content = citation.get("content")
            if content is None:
                body = (
                    f"# {_label(title)}\n\n"
                    "Source preview omitted by the publication policy. "
                    "The captured location is retained.\n"
                )
            else:
                fence = fence_marker(str(content).splitlines())
                language = extension_to_language_map("chunker").get(
                    PurePosixPath(source_file).suffix.lower(), ""
                )
                excerpt = str(content) + ("" if str(content).endswith("\n") else "\n")
                body = f"# {_label(title)}\n\n{fence}{language}\n{excerpt}{fence}\n"
                source_meta["x_codenib"]["content_sha256"] = hashlib.sha256(
                    str(content).encode("utf-8")
                ).hexdigest()
            files[reference] = _document(source_meta, body)
            references[reference] = (title, f"Captured source from {source_file}")
            resource = _relative(reference, path)
            if not any(source["id"] == source_id for source in sources):
                sources.append({"id": source_id, "resource": resource, "title": title})
            marker = (
                str(items[index - 1].get("id") or f"E{index}")
                if index <= len(items)
                else f"E{index}"
            )
            markers[marker] = (source_id, resource)
        for relation in evidence.get("relations") or []:
            marker = str(relation.get("id") or "")
            source_id = (
                "relation-"
                + hashlib.sha256(_json([source_fingerprint, relation])).hexdigest()[:24]
            )
            resource = _relative(evidence_path, path)
            sources.append(
                {
                    "id": source_id,
                    "resource": resource,
                    "title": f"Recorded static reference {marker}",
                }
            )
            markers[marker] = (source_id, resource)

        body = _rewrite_markdown(str(page.get("markdown") or ""), path, paths, markers)
        if sources:
            body += "\n\n## Sources\n\n" + "\n".join(
                f"[^{source['id']}]: [{_label(source['title'])}]({source['resource']})"
                for source in sources
            )
        metadata: dict[str, Any] = {
            "type": "Code Wiki Chapter",
            "title": str(page.get("title") or page_id),
            "description": summaries[page_id],
            "sources": sources,
            "x_codenib": {
                "page_id": page_id,
                "repository": repository,
                "commit": commit,
                "source_fingerprint": source_fingerprint,
                "evidence": _relative(evidence_path, path),
                "generation": page.get("generation") or {},
            },
        }
        # A passed citation/quality check is not a complete claim verification.
        # Preserve its actual verdict in the sidecar instead of minting verified.
        model = (page.get("generation") or {}).get("model")
        if model:
            metadata["generated"] = {"by": f"codenib/{model}"}
        files[path] = _document(metadata, body)

    if references:
        directories["references"] = [
            (path, *value) for path, value in sorted(references.items())
        ]
        directories[""].append(
            (
                "references/index.md",
                "Source references",
                "Captured source excerpts and version identities",
            )
        )
    directories["evidence"] = [
        (
            f"evidence/{page_id}.json",
            str(page_by_id[page_id].get("title") or page_id),
            "Recorded evidence, generation and check results",
        )
        for page_id in paths
    ]
    for directory, entries in directories.items():
        index_path = posixpath.join(directory, "index.md")
        body = (
            "# "
            + (
                "Source references"
                if directory == "references"
                else _label(repository) + " Wiki"
            )
            + "\n\n"
        )
        body += (
            "\n".join(
                f"- [{_label(title)}]({_relative(target, index_path)}) — {description}"
                for target, title, description in entries
            )
            + "\n"
        )
        files[index_path] = (
            _document({"okf_version": OKF_VERSION}, body)
            if not directory
            else body.encode("utf-8")
        )
    day = datetime.fromisoformat(snapshot_at.replace("Z", "+00:00")).date().isoformat()
    files["log.md"] = (
        f"# Directory Update Log\n\n## {day}\n\n"
        f"- **Snapshot**: {len(pages)} ready chapters bound to the source "
        "indexed on this date.\n"
        f"- Source commit: `{commit}`; source fingerprint: `{source_fingerprint}`.\n"
        "- This bundle records one snapshot; export does not regenerate or verify claims.\n"
    ).encode("utf-8")
    return files
