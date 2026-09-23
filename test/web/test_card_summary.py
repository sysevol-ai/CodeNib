# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

from codenib.web.card_summary import card_summary, is_purpose_sentence, manifest_summary


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
