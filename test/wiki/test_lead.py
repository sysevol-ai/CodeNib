# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from codenib.wiki.lead import overview_lead


def test_overview_lead_takes_first_prose_paragraph_without_citations():
    markdown = (
        "# Overview\n\n"
        "> **Reader question:** How? [E1](#evidence-E1)\n\n"
        "Requests turns a simple `requests.get()` call into an HTTP exchange."
        " [E8](#evidence-E8)\n\n"
        "## Next\n\nMore."
    )
    assert overview_lead(markdown) == (
        "Requests turns a simple `requests.get()` call into an HTTP exchange."
    )


def test_overview_lead_cuts_long_paragraphs_at_a_sentence():
    sentence = "Tokio schedules tasks across worker threads. "
    lead = overview_lead(sentence * 8)
    assert lead.endswith("threads.")
    assert len(lead) <= 220


def test_overview_lead_is_none_without_prose():
    assert overview_lead("# Overview\n\n## Only headings\n\n- a list") is None
