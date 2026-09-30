# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

from codenib.storage import SQLiteWikiStore
from scripts import backfill_visitor_wiki_graphs as command


def test_dry_run_keeps_source_database_and_files_unchanged(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "wiki_cache" / "visitor_wiki.sqlite3"
    store = SQLiteWikiStore(path)
    attempt = "a" * 64
    store.publish(
        entry_id=f"visitor:{attempt}",
        repository_id="visitor-wiki-attempts-v1",
        envelope={
            "data": {
                "id": attempt,
                "repository": "owner/repo",
                "commit": "b" * 40,
                "page_states": {"overview": "ready"},
            }
        },
    )
    before = {
        str(p.relative_to(tmp_path)): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    monkeypatch.setattr(
        command, "load_config", lambda _: SimpleNamespace(data_dir=tmp_path)
    )
    monkeypatch.setattr(
        SQLiteWikiStore,
        "__init__",
        lambda *_: (_ for _ in ()).throw(
            AssertionError("Dry runs must not initialize the source store")
        ),
    )
    assert command.main(["--dry-run", "--attempt", attempt]) == 0
    assert "owner/repo (not_indexed)" in capsys.readouterr().out
    after = {
        str(p.relative_to(tmp_path)): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    assert after == before
