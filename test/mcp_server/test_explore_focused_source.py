# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Deep matching source must survive a correctly ranked long anchor."""

from pathlib import Path

import pytest

from codenib.compiler.manifest import RepoManifest
from codenib.mcp.context import ServerContext
from codenib.mcp.tools.explore import explore_context_impl
from codenib.mcp.tools.source import read_source_excerpt_impl, read_source_impl
from codenib.source_fingerprint import capture_repository_source
from codenib.types import NODE_TYPE_FUNCTION


@pytest.fixture
def source(tmp_path, monkeypatch, request):
    lines = ["# prefix"] * 10 + ["def aggregate(items):"] + ["    # filler"] * 299
    lines[259] = "    groupedOptions = groupBy(items).reduce()"
    if getattr(request, "param", None) == "long_line":
        lines[259] = "    groupedOptions = '" + "x" * 20000 + "'"
    if getattr(request, "param", None) in ("late_single", "late_multi"):
        size = 8000 if request.param == "late_single" else 3000
        lines[259] = "x" * size + " groupedOptions reduce"
        lines[99] = "    groupedOptions = True"
    lines += ["# outside"] * 9 + [
        "def outside():",
        "    return 'groupBy groupedOptions reduce externalOnly'",
    ]
    (tmp_path / "long.py").write_text("\n".join(lines) + "\n")
    binding = capture_repository_source(tmp_path)
    ctx = ServerContext.load(
        RepoManifest(
            repo_path=str(tmp_path),
            commit="a" * 40,
            source_fingerprint=binding.fingerprint,
            file_count=binding.file_count,
        ),
        views=[],
        source_binding=binding,
    )
    ctx.vector = object()
    results = [
        {
            "file": "long.py",
            "node_name": "aggregate",
            "type": NODE_TYPE_FUNCTION,
            "start_line": 11,
            "end_line": 310,
            "content": "STALE_INDEX_BODY",
        }
    ]
    monkeypatch.setattr(
        "codenib.mcp.tools.explore.search_context_impl",
        lambda *_args, **_kwargs: {"results": results},
    )
    try:
        yield ctx, lines, results
    finally:
        ctx.vector = None
        ctx.close()


@pytest.mark.parametrize(
    "budget,limit", [("fast", 40), ("balanced", 80), ("thorough", 120)]
)
def test_explore_delivers_deep_match_with_original_line_bounds(source, budget, limit):
    ctx, lines, _ = source
    result = explore_context_impl(
        ctx,
        query="groupBy groupedOptions reduce externalOnly",
        budget=budget,
        include_dependencies=False,
    )
    context = result["files"][0]["contexts"][0]
    assert context["verified"] is True
    assert context["start_line"] == 260
    assert context["end_line"] <= 310
    assert context["end_line"] - context["start_line"] + 1 <= limit
    assert context["content"].splitlines()[0] == lines[259]
    assert "externalOnly" not in context["content"]
    assert "STALE_INDEX_BODY" not in context["content"]
    assert context["anchors"][0]["start_line"] == 11
    selection = context["source_projection"]["selection"]
    assert selection["scope_start_line"] == 11 and selection["scope_end_line"] == 310
    assert selection["omitted_before_lines"] == 249
    assert selection["matched_query_terms"] == 3
    assert selection["omitted_after_lines"] == 310 - context["end_line"]
    # Ordinary source reads keep their sequential window and validation.
    assert "groupedOptions" not in read_source_impl(ctx, "long.py", 11, 50)["content"]
    with pytest.raises(ValueError, match="at most 200 lines"):
        read_source_impl(ctx, "long.py", 11, 310)


def test_no_query_match_keeps_a_labelled_scope_head(source):
    ctx, _, _ = source
    result = explore_context_impl(
        ctx,
        query="unrelated missing operation",
        budget="fast",
        include_dependencies=False,
    )
    context = result["files"][0]["contexts"][0]
    assert [context["start_line"], context["end_line"]] == [11, 50]
    assert context["source_projection"]["selection"]["strategy"] == "scope_head"
    assert context["source_projection"]["selection"]["matched_query_terms"] == 0


def test_long_focus_cannot_claim_a_neighbour_lost_when_the_window_moves(source):
    ctx, _, results = source
    results.append(
        {
            "file": "long.py",
            "node_name": "nested",
            "type": NODE_TYPE_FUNCTION,
            "start_line": 20,
            "end_line": 21,
        }
    )
    result = explore_context_impl(
        ctx, query="groupedOptions reduce", budget="fast", include_dependencies=False
    )
    contexts = result["files"][0]["contexts"]
    assert len(contexts) == 2
    assert contexts[0]["start_line"] == 260
    assert contexts[1]["start_line"] <= 20 <= contexts[1]["end_line"]
    assert [c["anchors"][0]["symbol"] for c in contexts] == ["aggregate", "nested"]


def test_source_change_never_turns_cached_index_text_into_verified_evidence(source):
    ctx, _, _ = source
    (Path(ctx.manifest.repo_path) / "long.py").write_text("changed = True\n")
    result = explore_context_impl(
        ctx, query="groupedOptions reduce", budget="fast", include_dependencies=False
    )
    context = result["files"][0]["contexts"][0]
    assert context["verified"] is False and context["content_kind"] == "indexed_excerpt"
    assert any(d["code"] == "source_read_failed" for d in result["diagnostics"])


@pytest.mark.parametrize("source", ["long_line"], indirect=True)
def test_focused_read_retains_character_bound_and_reports_truncation(source):
    ctx, _, _ = source
    # The scope is 300 lines but the chosen line itself exceeds the byte/char
    # output cap. No character-limit bypass is permitted for a matching line.
    result = read_source_excerpt_impl(
        ctx, "long.py", 11, 310, query="groupedOptions", window_lines=40
    )
    assert result["start_line"] == result["end_line"] == 260
    assert len(result["content"]) == 16000
    assert result["content_projection"]["line_truncated"] is True
    assert result["content_projection"]["selection"]["scope_end_line"] == 310


@pytest.mark.parametrize("limit", [0, 201, True])
def test_excerpt_cannot_bypass_the_source_line_cap(source, limit):
    ctx, _, _ = source
    with pytest.raises(ValueError, match="window_lines must be between"):
        read_source_excerpt_impl(
            ctx, "long.py", 1, 300, query="groupedOptions", window_lines=limit
        )


def test_focus_projection_marks_omitted_scope_lines(source):
    ctx, _, _ = source
    result = read_source_excerpt_impl(
        ctx, "long.py", 11, 310, query="groupedOptions reduce", window_lines=40
    )
    projection = result["content_projection"]
    assert projection["truncated"] is True
    assert projection["strategy"] == "query_window"
    assert projection["selection"]["omitted_before_lines"] > 0


def test_focus_does_not_select_matches_beyond_character_delivery_limit():
    from codenib.context_delivery import query_focus_line

    invisible = "x" * 16001 + " groupedOptions reduce"
    visible = "return groupedOptions"
    assert query_focus_line(
        [invisible, visible], "groupedOptions reduce", max_line_chars=16000
    ) == (1, 1)
    assert query_focus_line(
        [invisible], "groupedOptions reduce", max_line_chars=16000
    ) == (0, 0)
    # A character cut must not fabricate an exact token from an identifier prefix.
    assert query_focus_line(
        ["groupedOptionsSuffix"], "groupedOptions", max_line_chars=len("groupedOptions")
    ) == (0, 0)


@pytest.mark.parametrize("source", ["late_single", "late_multi"], indirect=True)
def test_explore_focus_uses_the_final_per_context_projection_budget(source, request):
    ctx, _, rows = source
    multi = request.node.callspec.params["source"] == "late_multi"
    if multi:
        for line in (20, 150, 300):
            rows.append(
                {
                    "file": "long.py",
                    "node_name": f"other{line}",
                    "type": NODE_TYPE_FUNCTION,
                    "start_line": line,
                    "end_line": line + 1,
                }
            )
    result = explore_context_impl(
        ctx, query="groupedOptions reduce", budget="fast", include_dependencies=False
    )
    context = result["files"][0]["contexts"][0]
    assert context["start_line"] == 100
    assert context["content"].startswith("    groupedOptions = True")
    selection = context["source_projection"]["selection"]
    assert selection["matched_query_terms"] == 1
    assert selection["delivery_char_limit"] == (2000 if multi else 4800)
    assert len(context["content"]) <= selection["delivery_char_limit"]
