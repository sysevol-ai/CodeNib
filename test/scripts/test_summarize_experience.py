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


def test_install_copy_after_source_inspection_and_cli_first_runs(capsys):
    def event(name, visit, surface="landing"):
        return f"experience_event event={name} surface={surface} visit={visit * 32}"

    summarize(
        [
            event("example_source_open", "a"),
            event("install_command_copy", "a"),
            event("install_command_copy", "b"),
            event("guard_first_run", "c", "cli"),
        ]
    )
    cli, landing = capsys.readouterr().out.split("landing:")
    assert "cli: 1 observed tab visits" in cli and "guard_first_run: 1" in cli
    assert "install_command_copy: 2" in landing
    assert "source_inspection -> install_command_copy: 1/1" in landing


def test_arrivals_and_the_wikis_that_sent_them(capsys):
    def page_view(visit, detail="", surface="landing"):
        return (
            f"experience_event event=page_view surface={surface} "
            f"visit={visit * 32}{detail}"
        )

    summarize(
        [
            page_view("a", " arrival=wiki_link wiki=psf/requests"),
            page_view("a", " arrival=search"),  # A tab keeps its first arrival.
            page_view("b", " arrival=wiki_link wiki=psf/requests"),
            page_view("c", " arrival=wiki_link wiki=tokio"),
            page_view("d", " arrival=search"),
            page_view("e"),
            page_view("f", " arrival=wiki_link wiki=bad/name/extra"),  # Ignored.
            page_view("a", " arrival=social", "wiki"),
        ]
    )

    landing, wiki = capsys.readouterr().out.split("wiki:")
    assert "landing: 6 observed tab visits" in landing
    assert (
        "arrivals: search=1 social=0 github=0 wiki_link=3 other=0 direct_or_unknown=2"
        in landing
    )
    assert landing.index("wiki_link psf/requests: 2") < landing.index(
        "wiki_link tokio: 1"
    )
    assert "bad/name" not in landing
    assert "arrivals: search=0 social=1" in wiki
