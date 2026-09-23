# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

from codenib.web.card_summary import (
    card_summary,
    is_purpose_sentence,
    manifest_summary,
    project_names,
    readme_subject_sentence,
)


def test_support_and_behaviour_notes_are_not_purpose():
    assert not is_purpose_sentence("We have a community chat at Gitter. Feel free.")
    assert not is_purpose_sentence(
        "For questions and support please use the official forum or community chat."
    )
    assert not is_purpose_sentence("By default, bat pipes its own output to a pager.")
    assert is_purpose_sentence("Requests is a simple, yet elegant, HTTP library.")


def test_manifest_summary_reads_each_format():
    assert manifest_summary("package.json", '{"description": "A tiny DOM lib"}') == (
        "A tiny DOM lib"
    )
    assert (
        manifest_summary(
            "Cargo.toml", '[package]\nname = "bat"\ndescription = "A cat(1) clone"\n'
        )
        == "A cat(1) clone"
    )
    setup_py = (
        "class test(Command):\n    description = 'run all tests and doctests'\n\n"
        "setup(name='sympy',\n      description='Computer algebra system (CAS) in Python',\n)"
    )
    assert (
        manifest_summary("setup.py", setup_py)
        == "Computer algebra system (CAS) in Python"
    )


def test_card_summary_prefers_lead_then_manifest_then_readme(tmp_path):
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "bat"\ndescription = "A cat(1) clone with wings."\n'
    )
    bundle = SimpleNamespace(entry=SimpleNamespace(repo_dir=str(tmp_path)))
    readme = "By default, bat pipes its own output to a pager."
    assert card_summary(bundle, "bat highlights files as it prints them.", readme) == (
        "bat highlights files as it prints them."
    )
    assert card_summary(bundle, "We have a community chat at Gitter.", readme) == (
        "A cat(1) clone with wings."
    )
    empty = SimpleNamespace(entry=SimpleNamespace(repo_dir=str(tmp_path / "none")))
    assert card_summary(empty, None, readme) == ""


def test_project_names_cover_owner_style_names():
    assert project_names("vuejs/core") == ["core", "vuejs", "vue"]
    assert project_names("valkey-io/valkey") == ["valkey"]


def test_readme_subject_sentence_needs_the_project_as_subject():
    readme = (
        "# Valkey\n\nPlease make sure to respect issue requirements here today.\n\n"
        "Valkey is a high-performance data structure server for key/value data."
        " It also supports more.\n"
    )
    assert readme_subject_sentence(readme, ["valkey"]) == (
        "Valkey is a high-performance data structure server for key/value data."
    )
    sponsor = "Vue.js is an open source project made possible by its sponsors."
    assert readme_subject_sentence(sponsor, ["vue"]) == ""


def test_card_summary_reads_the_workspace_member_named_after_the_project(tmp_path):
    (tmp_path / "package.json").write_text('{"private": true}')
    member = tmp_path / "packages" / "vue"
    member.mkdir(parents=True)
    (member / "package.json").write_text(
        '{"description": "The progressive JavaScript framework for building web UI."}'
    )
    bundle = SimpleNamespace(
        entry=SimpleNamespace(repo_dir=str(tmp_path), repo="vuejs/core")
    )
    assert card_summary(bundle, None, "For questions and support use the forum.") == (
        "The progressive JavaScript framework for building web UI."
    )
