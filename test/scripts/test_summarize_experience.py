# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

from scripts.summarize_experience import summarize


def test_source_funnel_requires_a_later_step_in_the_same_tab(capsys):
    def event(name, visit, surface="landing"):
        return f"experience_event event={name} surface={surface} visit={visit * 32}"

    summarize(
        [
            "unrelated service log",
            event("repository_submit", "a"),
            event("example_source_open", "a"),
            event("agent_setup_open", "a"),
            event("example_source_open", "a"),  # Retain its first position.
            event("example_source_open", "b"),
            event("repository_submit", "b"),
            event("example_github_open", "b"),
            event("example_github_open", "c"),
            event("agent_setup_open", "c"),
            event("example_source_open", "c"),
            event("repository_submit", "d"),  # No source inspection.
            event("example_source_open", "a", "wiki"),
            event("repository_submit", "a", "wiki"),
        ]
    )

    landing, wiki = capsys.readouterr().out.split("wiki:")
    assert "landing: 4 observed tab visits" in landing
    assert "example_github_open: 2" in landing
    assert "source_inspection -> repository_submit: 1/3" in landing
    assert "source_inspection -> agent_setup_open: 2/3" in landing
    assert "source_inspection -> repository_submit: 1/1" in wiki
    assert "source_inspection -> agent_setup_open: 0/1" in wiki
