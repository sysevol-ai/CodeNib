# SPDX-FileCopyrightText: 2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import hashlib
import json
import posixpath
import re
from urllib.parse import unquote, urlsplit

import pytest
import yaml

from codenib.wiki.okf import render_okf_bundle


def _parse(payload):
    _, metadata, body = payload.decode().split("---", 2)
    return yaml.safe_load(metadata), body


def _bundle(markdown=None):
    excerpt = "def run():\n    return '``` [E99] (?p=missing)'\n\n"
    tree = [{"id": "overview", "children": [{"id": "runtime", "children": []}]}]
    pages = [
        {
            "id": "overview",
            "title": "Overview",
            "markdown": "# Overview\n\nGo to [`Runtime`](?p=runtime).",
            "citations": [],
        },
        {
            "id": "runtime",
            "title": "Runtime",
            "markdown": markdown
            or (
                "# Runtime\n\nThe runtime calls a helper. "
                "[E7](#evidence-E7) [R2](#evidence-R2)\n\n"
                "The same source is cited again. [E7]\n\n"
                "[Overview](?p=overview#overview)\n\n`[E99]`\n\n"
                "````python\n# [E99] [Example](?p=missing)\n````\n"
            ),
            "citations": [
                {
                    "file": "src/runtime.py",
                    "start_line": 1,
                    "end_line": 3,
                    "node_name": "run",
                    "content": excerpt,
                }
            ],
            "evidence": {
                "items": [
                    {
                        "id": "E7",
                        "file": "src/runtime.py",
                        "symbol": "run",
                        "node_id": 42,
                    }
                ],
                "relations": [
                    {
                        "id": "R2",
                        "source": "run",
                        "target": "helper",
                        "anchors": ["src/runtime.py:2"],
                    }
                ],
            },
            "generation": {"mode": "generated", "model": "example-model"},
            "grounding": {"valid": True},
        },
    ]
    return tree, pages, excerpt


def _render(tree, pages):
    return render_okf_bundle(
        tree,
        pages,
        repository="owner/repo",
        commit="a" * 40,
        source_fingerprint="sha256-v2:" + "b" * 64,
        snapshot_at="2026-10-29T22:00:00Z",
    )


def test_okf_conformance_navigation_and_exact_versioned_evidence():
    tree, pages, excerpt = _bundle()
    pages[1]["markdown"] += "\n`[Example](?p=missing)`\n"
    pages[1]["markdown"] += "\n``[Example](?p=missing)\n[E99]``\n"
    files = _render(tree, pages)
    assert files == _render(tree, pages)
    assert _parse(files["index.md"])[0] == {"okf_version": "0.2"}
    assert "## 2026-10-29" in files["log.md"].decode()
    for path, payload in files.items():
        if not path.endswith(".md") or posixpath.basename(path) in {
            "index.md",
            "log.md",
        }:
            continue
        metadata, _body = _parse(payload)
        assert metadata["type"]
        assert "verified" not in metadata
    meta, body = _parse(files["section-overview/page-runtime.md"])
    assert meta["generated"] == {"by": "codenib/example-model"}
    assert "[Overview](../page-overview.md#overview)" in body
    assert (
        "[`Runtime`](section-overview/page-runtime.md)"
        in files["page-overview.md"].decode()
    )
    assert "`[E99]`" in body
    assert "``[Example](?p=missing)\n[E99]``" in body
    assert "# [E99] [Example](?p=missing)" in body
    assert "[^source-" in body and "[^relation-" in body
    assert "#evidence-" not in body
    source = next(
        path
        for path in files
        if path.startswith("references/source-") and path.endswith(".md")
    )
    source_meta, source_body = _parse(files[source])
    assert excerpt in source_body
    assert source_meta["resource"] == "repo://src/runtime.py#L1-L3"
    assert (
        source_meta["x_codenib"]["content_sha256"]
        == hashlib.sha256(excerpt.encode()).hexdigest()
    )
    evidence = json.loads(files["evidence/runtime.json"])
    assert evidence["citations"][0]["content"] == excerpt
    assert evidence["evidence"]["items"][0]["node_id"] == 42
    assert evidence["evidence"]["relations"][0]["anchors"] == ["src/runtime.py:2"]
    assert evidence["line_base"] == 1 and evidence["end_line"] == "inclusive"
    for path, payload in files.items():
        if not path.endswith(".md"):
            continue
        for destination in re.findall(r"\]\(([^\s)]+)\)", payload.decode()):
            url = urlsplit(destination)
            if url.scheme or url.query:  # Literal query links inside code examples.
                continue
            target = posixpath.normpath(
                posixpath.join(posixpath.dirname(path), unquote(url.path))
            )
            assert target in files, (path, target)
    assert all(
        f"{directory}/index.md" in files
        for directory in ("section-overview", "references", "evidence")
    )


@pytest.mark.parametrize(
    "markdown",
    [
        "An unsupported claim. [E99]",
        "An unsupported claim. [E99](#evidence-E99)",
        "[Missing](?p=missing)",
    ],
)
def test_okf_refuses_unresolved_prose_references(markdown):
    tree, pages, _ = _bundle(markdown)
    with pytest.raises(ValueError, match="unavailable page|no captured evidence"):
        _render(tree, pages)


def test_okf_unanchored_and_omitted_preview_retains_identity():
    tree, pages, _ = _bundle()
    citation = pages[1]["citations"][0]
    citation.update(start_line=None, end_line=None, content=None, preview_omitted=True)
    files = _render(tree, pages)
    source = next(
        path
        for path in files
        if path.startswith("references/source-") and path.endswith(".md")
    )
    meta, body = _parse(files[source])
    assert meta["resource"] == "repo://src/runtime.py"
    assert meta["x_codenib"]["start_line"] is None
    assert "omitted" in body
    assert "content_sha256" not in meta["x_codenib"]


@pytest.mark.parametrize("page_id", ["..", "../escape", "", "a/b"])
def test_okf_refuses_unsafe_outline_paths(page_id):
    tree, pages, _ = _bundle()
    tree[0]["id"] = page_id
    with pytest.raises(ValueError, match="safe path"):
        _render(tree, pages)
