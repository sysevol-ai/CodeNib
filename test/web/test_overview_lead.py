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
