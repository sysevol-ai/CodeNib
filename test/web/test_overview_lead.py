# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

from codenib.web.overview_lead import replace_unfit_lead

GITTER = (
    "# Overview\n\nWe have a community chat at Gitter. Feel free to ask us anything there. "
    "[E1](#evidence-E1)\n\n## Expression Construction\n\nUsers build expressions. [E2](#evidence-E2)"
)


def test_a_support_note_gives_way_to_the_project_purpose():
    out = replace_unfit_lead(GITTER, "Computer algebra system (CAS) in Python")
    assert out.startswith(
        "# Overview\n\nComputer algebra system (CAS) in Python.\n\n## Expression"
    )
    assert "Gitter" not in out


def test_with_no_purpose_the_note_is_dropped():
    out = replace_unfit_lead(GITTER, "")
    assert out.startswith("# Overview\n\n## Expression Construction")


def test_a_lead_that_says_what_the_project_is_stays():
    markdown = "# Overview\n\nRequests sends HTTP/1.1 requests from Python. [E1](#evidence-E1)\n\n## A"
    assert replace_unfit_lead(markdown, "Something else.") == markdown


def test_a_feature_bullet_or_an_emoji_title_is_not_a_lead():
    bullet = "# Overview\n\n✨ Simplicity: the logic for agents fits in ~1,000 lines of code.\n\n## A"
    out = replace_unfit_lead(bullet, "Barebones library for agents that think in code.")
    assert out.startswith(
        "# Overview\n\nBarebones library for agents that think in code.\n\n## A"
    )
    title = "# Overview\n\nAMA-Bench is an 🧠 AMA-Bench: evaluating memory. [E1](#evidence-E1)\n\n## A"
    assert "🧠" not in replace_unfit_lead(
        title, "Evaluating long-horizon memory for agentic applications."
    )


def test_a_lead_that_says_the_same_thing_twice_keeps_the_fuller_sentence():
    markdown = (
        "# Overview\n\njq is a command-line JSON processor akin to sed, awk, grep, and friends for JSON data. "
        "[E1](#evidence-E1) `jq` is a portable C command-line JSON processor that applies a filter program "
        "to JSON inputs and writes the filter's results as JSON. [E1](#evidence-E1)\n\n## A"
    )
    out = replace_unfit_lead(markdown, "")
    assert "akin to sed" not in out
    assert "portable C command-line JSON processor" in out and out.count("[E1]") == 1


def test_two_different_lead_sentences_both_stay():
    markdown = (
        "# Overview\n\nRedis is an in-memory data structure server. [E1](#evidence-E1) "
        "Clients send commands over a socket and the event loop runs them. [E2](#evidence-E2)\n\n## A"
    )
    assert replace_unfit_lead(markdown, "") == markdown
