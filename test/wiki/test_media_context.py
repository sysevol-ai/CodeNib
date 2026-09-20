# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

from codenib.source_fingerprint import capture_repository_source
from codenib.wiki.media_context import visual_document_contexts


def test_context_uses_document_section_and_existing_source_paths(tmp_path):
    (tmp_path / "src" / "queue").mkdir(parents=True)
    (tmp_path / "src" / "queue" / "worker.py").write_text("def work(): pass\n")
    (tmp_path / "README.md").write_text(
        "# Service\n## Queue delivery\n```\n# Not a heading\n```\n"
        "The worker is src/queue/worker.py.\n![Flow](flow.svg)\n"
        "Retries wait for acknowledgement.\n"
    )
    persisted = {
        "media_manifest": {
            "artifacts": [
                {
                    "path": "flow.svg",
                    "caption": "Queue flow",
                    "references": [
                        {"markdown_path": "../private.md", "line": 1},
                        {
                            "markdown_path": "README.md",
                            "line": 7,
                            "alt_text": "Delivery flow",
                        },
                    ],
                }
            ]
        }
    }
    facts = [{"artifact_path": "flow.svg", "entities": [{"name": "src/absent.py"}]}]
    with capture_repository_source(tmp_path) as binding:
        bundle = SimpleNamespace(
            source_reader=binding.borrow_reader(),
            source_read_session=binding.read_session,
        )
        result = visual_document_contexts(persisted, facts, bundle)["flow.svg"]
    assert result["source_paths"] == ["src/queue/worker.py"]
    assert len(result["references"]) == 1
    reference = result["references"][0]
    assert reference["section"] == "Service / Queue delivery"
    assert reference["line"] == 7
    assert "acknowledgement" in reference["excerpt"]


def test_context_limits_document_reads_even_for_oversized_documents():
    calls = []

    class Reader:
        def captured_relative_path(self, path):
            return path

        def read_prefix(self, path, *, max_bytes):
            calls.append(path)
            return b"x" * max_bytes

    artifacts = [
        {
            "path": f"{i}.svg",
            "references": [
                {"markdown_path": f"{i}.md", "line": 1},
                {"markdown_path": f"{i}.md", "line": 1},
            ],
        }
        for i in range(12)
    ]
    result = visual_document_contexts(
        {"media_manifest": {"artifacts": artifacts}},
        [{"artifact_path": item["path"], "entities": []} for item in artifacts],
        SimpleNamespace(source_reader=Reader()),
    )
    assert len(calls) == 8
    assert len(set(calls)) == 8
    assert all(not context["references"] for context in result.values())
