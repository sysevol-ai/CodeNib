# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import json
from types import SimpleNamespace

import pytest

from codenib.wiki.agent_wiki import (
    AgentWiki,
    _drop_duplicate_framing,
    _ensure_cited_intro,
    _fact_plan_markdown,
    _readme_sentence_without_praise,
    _without_path_only_sections,
    _without_repeated_subject,
)
from codenib.wiki.evidence import EvidenceItem
from codenib.wiki.multimodal import plan_media_slots
from codenib.wiki.quality import (
    audit_page,
    page_quality_report,
    readability_report,
    reader_review_valid,
    reader_review_weak,
)
from codenib.wiki.visual_ir import flow_contract_from_relations, render_contract_report

PLAIN_PAGE = """\
Incoming requests pass through a router before any handler runs. [E1]

## Choosing a handler

The router looks at the path and picks the first handler that matches. [E1]
A missing match ends the request with a clear error for the caller. [E1]
`Router.match` holds this rule, so tests can exercise it alone. [E1]
Handlers never see a request that failed this step. [E1]
"""


def _padded(text):
    """Add plain prose so a test can look at one limit, not the minimum length."""

    return (
        text + "\n\n## More\n\nThe rest of this page explains how a router keeps its "
        "table of paths, and why a later match never replaces an earlier one. [E1]\n"
    )


def test_a_page_written_for_people_passes():
    report = readability_report(PLAIN_PAGE)

    assert report["readability_valid"] is True
    assert report["readability_failures"] == []
    assert report["opening_plain"] is True
    assert report["plain_sentence_ratio"] > 0.5


@pytest.mark.parametrize(
    "heading",
    ["Purpose and scope", "At a glance"],
)
def test_template_sections_fail(heading):
    report = readability_report(
        f"{PLAIN_PAGE}\n## {heading}\n\nA plain sentence here. [E1]"
    )

    assert report["readability_valid"] is False
    assert report["readability_failures"] == ["template_sections"]
    assert report["template_sections"] == [heading]


def test_a_printed_reader_question_fails():
    report = readability_report(
        PLAIN_PAGE.replace(
            "\n## Choosing",
            "\n> **Reader question:** How does routing work? [E1]\n\n## Choosing",
        )
    )

    assert "reader_question_callout" in report["readability_failures"]


def test_opening_on_an_identifier_fails():
    page = PLAIN_PAGE.replace(
        "Incoming requests pass through a router",
        "`Router.match` passes incoming requests",
    )

    report = readability_report(page)

    assert "opening_not_plain" in report["readability_failures"]


def test_an_overview_opening_has_no_code_at_all():
    page = PLAIN_PAGE.replace("a router", "the `Router`", 1)

    assert readability_report(page)["opening_plain"] is True
    assert readability_report(page, overview=True)["opening_plain"] is False


@pytest.mark.parametrize("word", ["It", "They", "Also", "However"])
def test_an_opening_cannot_lean_on_a_sentence_that_is_not_there(word):
    page = PLAIN_PAGE.replace("Incoming requests pass", f"{word} pass requests")

    assert "opening_not_plain" in readability_report(page)["readability_failures"]


def test_the_title_is_not_the_opening():
    report = readability_report(f"# `Router` internals\n\n{PLAIN_PAGE}")

    assert report["opening_plain"] is True
    assert report["opening_sentence"].startswith("Incoming requests")


def test_a_catalog_of_identifiers_fails():
    catalog = "\n".join(
        f"`Thing{index}.run` calls `Other{index}.go` and `More{index}.do`. [E1]"
        for index in range(6)
    )

    report = readability_report(
        f"Requests enter here first of all. [E1]\n\n## Parts\n\n{catalog}"
    )

    assert {
        "code_density",
        "code_led_sentences",
        "too_few_plain_sentences",
    } <= set(report["readability_failures"])


def test_ratios_do_not_judge_a_short_page():
    report = readability_report(
        "Requests enter here first of all, before any handler sees a single one "
        "of them, and the router decides what happens next on its own. [E1]\n\n"
        "## Parts\n\n`Router.match` picks a handler by path, and the first match "
        "wins outright. Nothing else can change the choice after that happens. [E1]"
    )

    assert report["readability_failures"] == []


def test_an_overview_main_path_counts_as_content():
    page = (
        "Requests is a Python HTTP library that sends requests for you. [E1]\n\n"
        "## From `get()` to `send()`\n\n"
        "1. **`get()`**: hands off to `request()`. [R1]\n"
        "2. **`request()`**: builds a request and sends it. [E2]\n"
        "3. **`send()`**: passes it to an adapter. [E3]\n"
    )

    assert readability_report(page, overview=True)["readability_failures"] == []
    assert "too_little_prose" in readability_report(page)["readability_failures"]


def test_a_page_with_almost_no_prose_fails():
    report = readability_report(
        "Construction of a request from user parameters. [E1]\n\n"
        "## Verified call path\n\n**Interactions**\n- `a()` → `b()` [R1]"
    )

    assert "too_little_prose" in report["readability_failures"]


def test_length_limits_differ_for_the_overview():
    sentence = "The router looks at each path and then picks a handler. [E1]\n"
    page = "Requests start at the router. [E1]\n\n## Flow\n\n" + sentence * 50

    assert "page_too_long" not in readability_report(page)["readability_failures"]
    assert (
        "page_too_long"
        in readability_report(page, overview=True)["readability_failures"]
    )


def test_long_sentences_fail():
    sentence = " ".join(["word"] * 50) + ". [E1]"

    report = readability_report(
        f"Requests enter here first of all. [E1]\n\n## Long\n\n{sentence}"
    )

    assert "sentence_too_long" in report["readability_failures"]


def test_excerpts_tables_and_navigation_are_not_prose():
    fence = "```python\n" + "\n".join("`x`" for _ in range(50)) + "\n```"
    table = "| Capability | Code |\n|---|---|\n| One | `a` |"
    page = f"{PLAIN_PAGE}\n{fence}\n\n{table}"

    assert readability_report(page) == readability_report(PLAIN_PAGE)


@pytest.mark.parametrize(
    ("review", "overview", "expected"),
    [
        (None, False, True),
        ({"score": 4}, False, True),
        ({"score": 3}, False, False),
        # The landing page is never withheld on a model's opinion.
        ({"score": 0}, True, True),
        # These flags earn a rewrite but do not withhold the page.
        ({"score": 8, "jargon": True}, False, True),
        ({"score": 8, "repetition": True}, False, True),
    ],
)
def test_reader_review_floor(review, overview, expected):
    assert reader_review_valid(review, overview=overview) is expected


@pytest.mark.parametrize(
    ("review", "overview", "expected"),
    [
        (None, False, False),
        ({"score": 8}, False, False),
        ({"score": 3}, False, True),
        ({"score": 3}, True, False),
        ({"score": 2}, True, True),
        ({"score": 8, "jargon": True}, False, True),
        ({"score": 8, "repetition": True}, True, True),
    ],
)
def test_weak_reviews_earn_a_rewrite(review, overview, expected):
    assert reader_review_weak(review, overview=overview) is expected


def test_a_cited_transition_line_is_not_glued_to_the_next_sentence():
    page = _padded(
        "Requests enter here first of all. [E1]\n\n## Parts\n\n"
        + "*After the jar fills up, the setter gives every caller a dict-like way "
        "to add one more cookie to it safely.* [E1](#evidence-E1) [E2](#evidence-E2)\n\n"
        + "The standard library has no such method, which makes cookies awkward "
        "for anyone who thinks of them as simple pairs of keys and values. [E1]"
    )

    assert "sentence_too_long" not in readability_report(page)["readability_failures"]


def test_quoted_code_is_not_a_style_problem():
    excerpt = "```python\n# better than the old way\nrun()\n```"

    detail = _audited(f"{PLAIN_PAGE}\n{excerpt}\n")

    assert detail["style_valid"] is True


def test_a_transition_line_is_not_glued_to_the_next_sentence():
    page = _padded(
        "Requests enter here first of all. [E1]\n\n## Parts\n\n"
        + "*After the jar fills up, the setter gives every caller a dict-like way "
        "to add one more cookie to it safely.*\n\n"
        + "The standard library has no such method, which makes cookies awkward "
        "for anyone who thinks of them as simple pairs of keys and values. [E1]"
    )

    assert "sentence_too_long" not in readability_report(page)["readability_failures"]


def _plan(statement):
    return {
        "thesis": {"statement": statement, "evidence": ["E1"]},
        "sections": [
            {
                "title": "Choosing a handler",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": "The router picks a handler for the request",
                        "evidence": ["E1"],
                    }
                ],
            }
        ],
    }


def test_the_gate_only_applies_when_required():
    page = (
        "`Router` picks a handler for every request. [E1]\n\n"
        "## Choosing\n\nThe router chooses one for the request. [E1]\n"
    )
    plan = _plan("`Router` picks a handler")

    relaxed = page_quality_report(page, plan)
    strict = page_quality_report(page, plan, require_readability=True)

    assert relaxed["readability_valid"] is False
    assert relaxed["valid"] is True
    assert strict["valid"] is False


def _audited(markdown, **quality):
    return audit_page(
        {
            "id": "routing",
            "title": "Routing",
            "markdown": markdown,
            "grounding": {"valid": True},
            "quality": {"valid": True, **quality},
            "generation": {"mode": "generated"},
        }
    )


def test_audit_reports_older_pages_without_failing_them():
    detail = _audited("`Router` picks a handler for every request. [E1]")

    assert detail["readability_valid"] is False
    assert detail["readability_ready"] is True


def test_audit_fails_a_new_page_the_reader_review_rejected():
    detail = _audited(PLAIN_PAGE, require_readability=True, story_review={"score": 2})

    assert detail["readability_valid"] is True
    assert detail["reader_review_valid"] is False
    assert detail["readability_ready"] is False
    assert detail["publishable"] is False


_EVIDENCE = EvidenceItem(
    id="E1",
    file="src/core.py",
    start_line=1,
    end_line=4,
    symbol="Router",
    kind="class",
    content=(
        "class Router:\n"
        '    """Route each repository request to one handler.\n\n'
        "    The dispatch method passes the request to the handle function and\n"
        "    returns the value it gives back to the caller unchanged. Exactly one\n"
        '    handler sees any given request, never several."""\n'
        "    def dispatch(self, repository_request):\n"
        "        return handle(repository_request)"
    ),
)


def test_rendered_page_has_no_template_scaffolding():
    plan = {
        **_plan("The router sends each repository request to one handler"),
        "purpose": {
            "statements": ["Each repository request reaches one handler"],
            "evidence": ["E1"],
        },
        "map": [{"concern": "Routing", "entity": "`Router`", "evidence": ["E1"]}],
        "story": {
            "reader_question": {
                "statement": "How does the router send a request?",
                "evidence": ["E1"],
            }
        },
    }

    markdown = _fact_plan_markdown(plan, [_EVIDENCE], [])

    assert "Purpose and scope" not in markdown
    assert "At a glance" not in markdown
    assert "Reader question" not in markdown
    assert "| Capability |" not in markdown


def test_a_plain_purpose_sentence_opens_instead_of_an_identifier():
    plan = {
        **_plan("`Router` dispatches each repository request"),
        "purpose": {
            "statements": ["Each repository request reaches one handler"],
            "evidence": ["E1"],
        },
    }

    markdown = _fact_plan_markdown(plan, [_EVIDENCE], [])

    assert markdown.startswith("Each repository request reaches one handler.")
    assert "`Router` dispatches each repository request" in markdown


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Requests is a simple, yet elegant, HTTP library.",
            "Requests is an HTTP library.",
        ),
        (
            "jq is a lightweight and flexible command-line JSON processor.",
            "jq is a command-line JSON processor.",
        ),
        (
            "Ruff is an extremely fast Python linter, written in Rust.",
            "Ruff is a Python linter, written in Rust.",
        ),
        # Nothing to remove, or nothing left worth saying.
        ("Vue is a JavaScript framework.", ""),
        ("jq is lightweight.", ""),
    ],
)
def test_praise_is_removed_without_losing_the_noun(text, expected):
    assert _readme_sentence_without_praise(text) == expected


def _readme(text):
    return EvidenceItem(
        id="E1",
        file="README.md",
        start_line=1,
        end_line=2,
        symbol="README.md",
        kind="file",
        content=text,
    )


_PRAISE = "Requests is a simple, yet elegant, HTTP library."


def test_a_short_readme_sentence_keeps_the_thesis_beside_it():
    rendered = _ensure_cited_intro(
        "`Session.send()` passes each prepared request to a transport adapter. [E2]\n\n"
        "## Flow\n\nBody text here. [E2]",
        [_readme(_PRAISE)],
        canonical_readme=True,
    )

    assert rendered.startswith(
        "Requests is an HTTP library. [E1] `Session.send()` passes each prepared"
    )


def test_a_plain_thesis_is_not_preceded_by_the_readme_sentence():
    draft = (
        "Requests lets Python programs send HTTP requests and read the replies. [E1]"
        "\n\n## Flow\n\nBody text here. [E1]"
    )

    assert (
        _ensure_cited_intro(draft, [_readme(_PRAISE)], canonical_readme=True) == draft
    )


def test_applying_the_readme_sentence_twice_changes_nothing():
    once = _ensure_cited_intro(
        "`Session.send()` passes each prepared request onward. [E2]\n\n## Flow\n\nBody.",
        [_readme(_PRAISE)],
        canonical_readme=True,
    )

    assert _ensure_cited_intro(once, [_readme(_PRAISE)], canonical_readme=True) == once


def test_a_stage_does_not_repeat_the_name_its_row_shows():
    assert (
        _without_repeated_subject("`request()` builds a Request", "request()")
        == "Builds a Request"
    )
    assert (
        _without_repeated_subject("It builds a Request", "request()")
        == "It builds a Request"
    )


class _Client:
    def __init__(self, statements):
        self.statements = statements
        self.prompts = []

    def complete(self, messages, **_kwargs):
        self.prompts.append(messages[0]["content"])
        return json.dumps({"statements": self.statements})


def _wiki(client, tmp_path):
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        manifest=SimpleNamespace(languages=["python"], indexes={}),
        code_graph=lambda: None,
    )
    return AgentWiki(bundle, model="fake", llm=client)


def test_rewrite_changes_wording_only_when_the_source_supports_it(tmp_path):
    client = _Client(
        [
            {
                "id": "thesis",
                "statement": "Each repository request reaches one handler",
            },
            {"id": "c0.0", "statement": "The billing engine retries failed payments"},
        ]
    )
    wiki = _wiki(client, tmp_path)
    plan = _plan("`Router` dispatches each repository request")

    rewritten = wiki._rewrite_for_readers(
        {"title": "Routing"},
        plan,
        ["opening_not_plain"],
        [_EVIDENCE],
        [],
        overview=False,
    )

    assert (
        rewritten["thesis"]["statement"]
        == "Each repository request reaches one handler"
    )
    # The invented claim keeps the sentence the plan already admitted.
    assert (
        rewritten["sections"][0]["claims"][0]["statement"]
        == "The router picks a handler for the request"
    )
    # The caller's plan is untouched until the new text proves better.
    assert plan["thesis"]["statement"].startswith("`Router`")
    assert "opening_not_plain" in client.prompts[0]


def test_rewrite_reaches_the_purpose_and_section_leads(tmp_path):
    client = _Client(
        [
            {"id": "p.0", "statement": "Each repository request reaches one handler"},
            {"id": "l0.0", "statement": "One handler sees each repository request"},
        ]
    )
    plan = {
        **_plan("Each repository request is routed by the class"),
        "purpose": {
            "statements": ["`Router` routes each repository request"],
            "evidence": ["E1"],
        },
    }
    plan["sections"][0]["lead"] = {
        "statements": ["`Router` picks one handler"],
        "evidence": ["E1"],
    }

    rewritten = _wiki(client, tmp_path)._rewrite_for_readers(
        {"title": "Routing"},
        plan,
        ["code_led_sentences"],
        [_EVIDENCE],
        [],
        overview=False,
    )

    assert rewritten["purpose"]["statements"] == [
        "Each repository request reaches one handler"
    ]
    assert rewritten["sections"][0]["lead"]["statements"] == [
        "One handler sees each repository request"
    ]


def test_rewrite_refuses_to_turn_one_statement_into_several(tmp_path):
    client = _Client(
        [
            {
                "id": "thesis",
                "statement": "Each repository request reaches one handler. It returns.",
            }
        ]
    )

    assert (
        _wiki(client, tmp_path)._rewrite_for_readers(
            {"title": "Routing"},
            _plan("`Router` dispatches each repository request"),
            ["opening_not_plain"],
            [_EVIDENCE],
            [],
            overview=False,
        )
        is None
    )


def test_rewrite_leaves_flow_claims_alone(tmp_path):
    client = _Client([{"id": "c0.0", "statement": "One handler sees each request"}])
    plan = _plan("`Router` dispatches each repository request")
    plan["sections"][0]["claims"][0].update(
        {"role": "flow", "statement": "`Router` calls `handle`"}
    )

    assert (
        _wiki(client, tmp_path)._rewrite_for_readers(
            {"title": "Routing"},
            plan,
            ["code_led_sentences"],
            [_EVIDENCE],
            [],
            overview=False,
        )
        is None
    )
    assert '"c0.0"' not in client.prompts[0]


@pytest.mark.parametrize("reply", ["not json", '{"statements": []}'])
def test_rewrite_gives_up_quietly(reply, tmp_path):
    client = SimpleNamespace(complete=lambda *_args, **_kwargs: reply)
    wiki = _wiki(client, tmp_path)

    assert (
        wiki._rewrite_for_readers(
            {"title": "Routing"},
            _plan("`Router` dispatches each repository request"),
            ["opening_not_plain"],
            [_EVIDENCE],
            [],
            overview=False,
        )
        is None
    )


_CALLS = [
    {"id": "R1", "source": "a.py:Api.get()", "target": "b.py:Session.send()"},
    {"id": "R2", "source": "b.py:Session.send()", "target": "c.py:Adapter.send()"},
    {"id": "R3", "source": "c.py:Adapter.send()", "target": "d.py:build()"},
]


def test_the_longest_recorded_call_path_is_drawn():
    contract = flow_contract_from_relations(
        [
            {"id": "R9", "source": "z.py:other()", "target": "c.py:Adapter.send()"},
            *_CALLS,
        ]
    )

    assert [node["label"] for node in contract["data"]["nodes"]] == [
        "Api.get()",
        "Session.send()",
        "Adapter.send()",
        "build()",
    ]
    assert [edge["evidence"] for edge in contract["data"]["edges"]] == [
        ["R1"],
        ["R2"],
        ["R3"],
    ]
    assert render_contract_report(contract, allowed_evidence=["R1", "R2", "R3"])[
        "valid"
    ]


@pytest.mark.parametrize(
    "relations",
    [
        [],
        _CALLS[:1],
        # A reference to a type is not a step in how the code runs.
        [
            {"id": "R1", "source": "a.py:go()", "target": "a.py:Config"},
            {"id": "R2", "source": "a.py:Config", "target": "a.py:load()"},
        ],
        # Neither is a test calling into the code.
        [
            {"id": "R1", "source": "tests/test_a.py:test_go()", "target": "a.py:go()"},
            {"id": "R2", "source": "a.py:go()", "target": "a.py:run()"},
        ],
        # An arrow with no recorded relation id cannot be cited.
        [
            {"source": "a.py:go()", "target": "a.py:run()"},
            {"source": "a.py:run()", "target": "a.py:save()"},
        ],
    ],
)
def test_nothing_is_drawn_without_a_real_call_path(relations):
    assert flow_contract_from_relations(relations) is None


def test_a_cycle_ends_the_path():
    contract = flow_contract_from_relations(
        [
            {"id": "R1", "source": "a.py:go()", "target": "a.py:run()"},
            {"id": "R2", "source": "a.py:run()", "target": "a.py:save()"},
            {"id": "R3", "source": "a.py:save()", "target": "a.py:go()"},
        ]
    )

    assert len(contract["data"]["nodes"]) == 3


def test_a_long_path_stays_within_the_figure_limit():
    relations = [
        {"id": f"R{i}", "source": f"a.py:f{i}()", "target": f"a.py:f{i + 1}()"}
        for i in range(12)
    ]

    assert len(flow_contract_from_relations(relations)["data"]["nodes"]) == 7


@pytest.mark.parametrize(
    ("tier", "drawn"),
    [("primary", True), ("key", True), ("detail", False), ("more", False)],
)
def test_only_important_pages_get_a_call_path(tier, drawn):
    slots = plan_media_slots(
        page_id="sessions",
        title="Sessions",
        relations=_CALLS,
        citations=[{"file": "a.py"}],
        tier=tier,
    )

    if not drawn:
        assert slots == []
        return
    assert [slot["id"] for slot in slots] == ["sessions-call-path"]
    assert slots[0]["title"] == "Selected call path: Api.get() → build()"
    assert slots[0]["placement"] == "aside"
    assert slots[0]["source_citations"] == ["a.py"]


def test_an_important_page_without_a_path_draws_nothing():
    assert (
        plan_media_slots(
            page_id="sessions",
            title="Sessions",
            relations=_CALLS[:1],
            tier="key",
        )
        == []
    )


_NODE = {
    "file": "src/core.py",
    "node_name": "Router",
    "type": "class",
    "start_line": 0,
    "end_line": 8,
    "content": _EVIDENCE.content,
}
_META = {
    "id": "routing",
    "title": "Request Routing",
    "summary": "How requests move through the repository",
    "keywords": ["dispatch"],
    "files": ["src/core.py"],
}


class _FakeVectorStore:
    def __init__(self, nodes):
        self.nodes = nodes

    def search_with_content(self, _query, top_k):
        return self.nodes[:top_k]


def _page_wiki(tmp_path, plan, client, *, story_review=False):
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=_FakeVectorStore([_NODE]),
        bm25=None,
        manifest=SimpleNamespace(languages=["python"], indexes={}),
        code_graph=lambda: None,
    )
    wiki = AgentWiki(bundle, model="fake", llm=client, story_review=story_review)
    wiki._fact_plan = lambda *_args: (plan, [])
    return wiki


def _review(score, **flags):
    per_question = [min(2, max(0, score - 2 * index)) for index in range(4)]
    names = ("problem", "path", "decision", "failure")
    return json.dumps(
        {
            **{
                name: {"score": value, "quote": ""}
                for name, value in zip(names, per_question, strict=True)
            },
            "jargon": False,
            "repetition": False,
            "notes": "Say what the router is for.",
            **flags,
        }
    )


class _ScriptedClient:
    """Answers rewrite and review prompts from separate scripts."""

    def __init__(self, *, rewrites=(), reviews=()):
        self.rewrites = list(rewrites)
        self.reviews = list(reviews)
        self.calls = []

    def complete(self, messages, **_kwargs):
        prompt = messages[0]["content"]
        if "wants to keep reading" in prompt:
            self.calls.append("rewrite")
            return json.dumps({"statements": self.rewrites.pop(0)})
        if "as a newcomer" in prompt:
            self.calls.append("review")
            return self.reviews.pop(0)
        raise AssertionError(f"unexpected prompt: {prompt[:80]}")


_READABLE = "Each repository request is routed to one handler by `Router`"
_CODE_LED = "`Router` routes each repository request to one handler"
_REWRITTEN = "Each repository request reaches one handler"


def _claim(statement):
    return {"role": "responsibility", "statement": statement, "evidence": ["E1"]}


def _page_plan(thesis):
    return {
        "thesis": {"statement": thesis, "evidence": ["E1"]},
        "sections": [
            {
                "title": "Dispatch",
                "claims": [
                    _claim(
                        "A repository request reaches the handle function through "
                        "the dispatch method on this class"
                    ),
                    _claim(
                        "The value that handle gives back goes straight to whoever "
                        "called dispatch, unchanged"
                    ),
                ],
            },
            {
                "title": "One handler per request",
                "claims": [
                    _claim(
                        "Routing here means exactly one handler sees any given "
                        "repository request, never several"
                    ),
                ],
            },
        ],
    }


def test_a_readable_page_is_published_without_extra_model_calls(tmp_path):
    client = _ScriptedClient()

    page = _page_wiki(tmp_path, _page_plan(_READABLE), client)._generate_page(_META)

    assert page["quality"]["valid"] is True
    assert page["quality"]["require_readability"] is True
    assert page["generation"]["mode"] == "generated"
    assert page["generation"]["repaired"] is False
    assert client.calls == []


def test_an_unreadable_page_is_rewritten_and_published(tmp_path):
    client = _ScriptedClient(
        rewrites=[[{"id": "thesis", "statement": _READABLE}]],
    )

    page = _page_wiki(tmp_path, _page_plan(_CODE_LED), client)._generate_page(_META)

    assert client.calls == ["rewrite"]
    assert page["quality"]["valid"] is True
    assert page["quality"]["opening_plain"] is True
    assert page["generation"]["repaired"] is True
    assert page["generation"]["mode"] == "generated"
    assert page["markdown"].split("\n\n")[1].startswith(_READABLE)


def test_a_page_the_rewrite_cannot_fix_is_not_published(tmp_path):
    client = _ScriptedClient(rewrites=[[]])

    page = _page_wiki(tmp_path, _page_plan(_CODE_LED), client)._generate_page(_META)

    assert page["quality"]["valid"] is False
    assert page["quality"]["readability_failures"] == ["opening_not_plain"]
    assert page["generation"]["mode"] == "degraded"
    assert page["generation"]["reason"] == "quality_guard"


def test_a_rewrite_with_invented_facts_cannot_pass_the_gate(tmp_path):
    client = _ScriptedClient(
        rewrites=[
            [{"id": "thesis", "statement": "The billing engine retries payments"}]
        ],
    )

    page = _page_wiki(tmp_path, _page_plan(_CODE_LED), client)._generate_page(_META)

    assert "billing" not in page["markdown"]
    assert page["generation"]["mode"] == "degraded"


def test_a_low_reader_review_earns_a_rewrite_that_can_save_the_page(tmp_path):
    client = _ScriptedClient(
        rewrites=[
            [
                {
                    "id": "thesis",
                    "statement": _REWRITTEN,
                }
            ]
        ],
        reviews=[_review(2), _review(6)],
    )

    page = _page_wiki(
        tmp_path, _page_plan(_READABLE), client, story_review=True
    )._generate_page(_META)

    assert client.calls == ["review", "rewrite", "review"]
    assert page["quality"]["story_review"]["score"] == 6
    assert page["quality"]["valid"] is True
    assert page["generation"]["mode"] == "generated"
    assert _REWRITTEN in page["markdown"]


def test_a_rewrite_the_reviewer_likes_less_is_discarded(tmp_path):
    client = _ScriptedClient(
        rewrites=[
            [
                {
                    "id": "thesis",
                    "statement": _REWRITTEN,
                }
            ]
        ],
        reviews=[_review(2), _review(1)],
    )

    page = _page_wiki(
        tmp_path, _page_plan(_READABLE), client, story_review=True
    )._generate_page(_META)

    assert page["quality"]["story_review"]["score"] == 2
    assert _REWRITTEN not in page["markdown"]
    assert page["quality"]["valid"] is False
    assert page["quality"]["reader_review_failed"] is True
    assert page["generation"]["mode"] == "degraded"


def test_a_good_review_needs_no_rewrite(tmp_path):
    client = _ScriptedClient(reviews=[_review(7)])

    page = _page_wiki(
        tmp_path, _page_plan(_READABLE), client, story_review=True
    )._generate_page(_META)

    assert client.calls == ["review"]
    assert page["generation"]["mode"] == "generated"


def test_repetition_alone_earns_a_rewrite_but_does_not_block(tmp_path):
    client = _ScriptedClient(
        rewrites=[[]],
        reviews=[_review(7, repetition=True)],
    )

    page = _page_wiki(
        tmp_path, _page_plan(_READABLE), client, story_review=True
    )._generate_page(_META)

    assert client.calls == ["review", "rewrite"]
    assert page["generation"]["mode"] == "generated"


def test_no_rewrite_is_attempted_when_the_model_failed(tmp_path):
    client = _ScriptedClient()
    wiki = _page_wiki(tmp_path, _page_plan(_CODE_LED), client)
    wiki._fact_plan = lambda *_args: (
        _page_plan(_CODE_LED),
        ["model planning unavailable"],
    )

    page = wiki._generate_page(_META)

    assert client.calls == []
    assert page["generation"]["reason"] == "model_unavailable"


def _flow_section(title):
    return {
        "title": title,
        "claims": [
            {"role": "flow", "statement": "`a()` calls `b()`", "evidence": ["R1"]}
        ],
    }


def test_sections_that_only_repeat_the_main_path_are_dropped():
    prose = {
        "title": "Dispatch",
        "claims": [{"role": "contract", "statement": "A rule", "evidence": ["E1"]}],
    }
    plan = {"sections": [_flow_section("Entry"), prose]}

    assert _without_path_only_sections(plan)["sections"] == [prose]


def test_a_page_keeps_a_section_even_if_it_only_lists_calls():
    plan = {"sections": [_flow_section("Entry")]}

    assert _without_path_only_sections(plan) is plan


def test_recorded_rows_keep_a_section_from_counting_as_thin():
    markdown = (
        "Requests lets programs send HTTP requests and read the replies back. [E1]\n\n"
        "## Entry\n\n**Interactions**\n- `get()` → `request()` [R1]\n\n"
        "## Dispatch\n\nThe session picks an adapter and sends the prepared "
        "request through it to the network. [E1]\n"
    )
    plan = {
        "thesis": {"statement": "Requests sends requests", "evidence": ["E1"]},
        "sections": [
            {"title": "Entry", "claims": [{"evidence": ["R1"], "statement": "x"}]},
            {"title": "Dispatch", "claims": [{"evidence": ["E1"], "statement": "y"}]},
        ],
    }

    report = page_quality_report(markdown, plan, require_dense_sections=True)

    assert report["thin_sections"] == []


def test_a_transition_restating_the_next_paragraph_is_dropped():
    framing = (
        "*Once the session has a merged jar, it hands that jar over to generate "
        "the Cookie header.* [E1]"
    )
    claim = (
        "The session merges request cookies into one jar, then hands it over to "
        "generate the Cookie header. [E1]"
    )

    assert _drop_duplicate_framing("Cookies", [framing, claim], [framing]) == [claim]


def test_a_transition_adding_something_new_stays():
    framing = (
        "*Authentication happens only after every other header has been decided.* [E1]"
    )
    claim = "The digest handler replays the request when a server answers 401. [E1]"

    assert _drop_duplicate_framing("Auth", [framing, claim], [framing]) == [
        framing,
        claim,
    ]


def test_a_sentence_needing_an_earlier_one_never_opens_the_page():
    plan = {
        **_plan("`Router` dispatches each repository request"),
        "purpose": {
            "statements": [
                "It also routes each repository request to one handler",
                "Each repository request reaches one handler",
            ],
            "evidence": ["E1"],
        },
    }

    markdown = _fact_plan_markdown(plan, [_EVIDENCE], [])

    assert markdown.startswith("Each repository request reaches one handler.")


def test_a_review_that_scores_nothing_is_not_a_verdict(tmp_path):
    client = _ScriptedClient(reviews=[_review(0)])

    page = _page_wiki(
        tmp_path, _page_plan(_READABLE), client, story_review=True
    )._generate_page(_META)

    assert "story_review" not in page["quality"]
    assert page["generation"]["mode"] == "generated"
