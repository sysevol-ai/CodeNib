# SPDX-FileCopyrightText: 2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from codenib.wiki.agent_pointer import write_agent_pointers


def _setup(tmp_path):
    bundle = tmp_path / "my wiki"
    bundle.mkdir()
    (bundle / "index.md").write_text("# Wiki\n")
    (bundle / "codenib-okf.json").write_text("{}\n")
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    return bundle, consumer


def test_pointer_preserves_user_text_updates_own_block_and_is_idempotent(tmp_path):
    bundle, consumer = _setup(tmp_path)
    agents = consumer / "AGENTS.md"
    agents.write_text("# User rules\n\nKeep these.\n")
    claude = consumer / "CLAUDE.md"
    claude.write_text(
        "Before\n<!-- BEGIN CODENIB WIKI -->\nold pointer\n<!-- END CODENIB WIKI -->\nAfter\n"
    )
    assert write_agent_pointers(bundle, consumer) == [agents, claude]
    assert agents.read_text().startswith("# User rules\n\nKeep these.\n")
    assert "../my%20wiki/index.md" in agents.read_text()
    assert claude.read_text().startswith("Before\n")
    assert claude.read_text().endswith("\nAfter\n")
    assert "old pointer" not in claude.read_text()
    before = {path: path.stat().st_mtime_ns for path in (agents, claude)}
    assert write_agent_pointers(bundle, consumer) == []
    assert before == {path: path.stat().st_mtime_ns for path in (agents, claude)}


def test_pointer_creates_agents_without_creating_claude(tmp_path):
    bundle, consumer = _setup(tmp_path)
    assert write_agent_pointers(bundle, consumer) == [consumer / "AGENTS.md"]
    assert not (consumer / "CLAUDE.md").exists()


@pytest.mark.parametrize(
    "content",
    [
        "<!-- BEGIN CODENIB WIKI -->",
        "<!-- END CODENIB WIKI -->\n<!-- BEGIN CODENIB WIKI -->",
        "<!-- BEGIN CODENIB WIKI --><!-- END CODENIB WIKI -->" * 2,
    ],
)
def test_malformed_pointer_refuses_all_edits(tmp_path, content):
    bundle, consumer = _setup(tmp_path)
    (consumer / "CLAUDE.md").write_text(content)
    with pytest.raises(ValueError, match="malformed"):
        write_agent_pointers(bundle, consumer)
    assert not (consumer / "AGENTS.md").exists()
    assert (consumer / "CLAUDE.md").read_text() == content


def test_pointer_refuses_bundle_mutation_and_symlink(tmp_path):
    bundle, consumer = _setup(tmp_path)
    with pytest.raises(ValueError, match="outside"):
        write_agent_pointers(bundle, bundle)
    target = tmp_path / "rules.md"
    target.write_text("keep\n")
    (consumer / "AGENTS.md").symlink_to(target)
    with pytest.raises(ValueError, match="symbolic link"):
        write_agent_pointers(bundle, consumer)
    assert target.read_text() == "keep\n"
