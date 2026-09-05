# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import audit_wiki_cache as audit_command
from scripts import prewarm_wiki_cache as prewarm_command


class _Registry:
    def __init__(self, *_args, **_kwargs):
        self.loaded = False

    def load_all(self):
        self.loaded = True


def _config(data_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        data_dir=str(data_dir),
        wiki_generation_model="fake-model",
        wiki_generation_api_base=None,
        wiki_generation_api_key=None,
        wiki_generation_options={},
    )


@pytest.mark.parametrize("database_exists", (False, True))
def test_audit_main_opens_only_an_existing_database_read_only(
    tmp_path,
    monkeypatch,
    database_exists,
):
    data_dir = tmp_path / "data"
    database_path = data_dir / "wiki_cache" / "wiki.sqlite3"
    if database_exists:
        database_path.parent.mkdir(parents=True)
        database_path.write_bytes(b"present")
    opened = []
    snapshot = object()

    class Store:
        def __init__(self, _path):
            raise AssertionError("audit must not construct a writable store")

        @classmethod
        @contextmanager
        def _read_only_snapshot(cls, path):
            opened.append(("enter", Path(path)))
            try:
                yield snapshot
            finally:
                opened.append(("exit", Path(path)))

    observed = {}

    def audit(_registry, *, model, repo_ids, store):
        observed.update(model=model, repo_ids=repo_ids, store=store)
        return {
            "missing_outlines": [],
            "missing_overviews": [],
            "fallback_pages": [],
            "quality_invalid_pages": [],
        }

    monkeypatch.setattr(audit_command, "load_config", lambda _path: _config(data_dir))
    monkeypatch.setattr(audit_command, "RepoRegistry", _Registry)
    monkeypatch.setattr(audit_command, "SQLiteWikiStore", Store)
    monkeypatch.setattr(audit_command, "audit_wiki_cache", audit)
    monkeypatch.setattr(audit_command, "set_console_log_level", lambda _level: None)

    assert audit_command.main(["--compact"]) == 0
    assert observed == {
        "model": "fake-model",
        "repo_ids": None,
        "store": snapshot if database_exists else None,
    }
    assert opened == (
        [("enter", database_path), ("exit", database_path)] if database_exists else []
    )
    if not database_exists:
        assert not database_path.parent.exists()


@pytest.mark.parametrize(
    ("database_exists", "arguments", "expected_mode"),
    (
        (True, ["--dry-run", "--compact"], "read-only"),
        (False, ["--dry-run", "--compact"], None),
        (False, ["--compact"], "writable"),
    ),
)
def test_prewarm_main_selects_store_mode(
    tmp_path,
    monkeypatch,
    database_exists,
    arguments,
    expected_mode,
):
    data_dir = tmp_path / "data"
    database_path = data_dir / "wiki_cache" / "wiki.sqlite3"
    if database_exists:
        database_path.parent.mkdir(parents=True)
        database_path.write_bytes(b"present")
    opened = []

    class Store:
        def __new__(cls, path):
            opened.append(("writable", Path(path)))
            return SimpleNamespace(mode="writable")

        @classmethod
        @contextmanager
        def _read_only_snapshot(cls, path):
            opened.append(("read-only-enter", Path(path)))
            try:
                yield SimpleNamespace(mode="read-only")
            finally:
                opened.append(("read-only-exit", Path(path)))

    factory_store = []

    class Wiki:
        def __init__(self, _bundle, *, store, **_kwargs):
            factory_store.append(store)

    def prewarm(_registry, *, wiki_factory, **_kwargs):
        wiki_factory(object())
        return {"pages": []}

    monkeypatch.setattr(
        prewarm_command,
        "load_config",
        lambda _path: _config(data_dir),
    )
    monkeypatch.setattr(prewarm_command, "RepoRegistry", _Registry)
    monkeypatch.setattr(prewarm_command, "SQLiteWikiStore", Store)
    monkeypatch.setattr(prewarm_command, "AgentWiki", Wiki)
    monkeypatch.setattr(prewarm_command, "LiteLLMChat", lambda **_kwargs: object())
    monkeypatch.setattr(prewarm_command, "prewarm_wiki_cache", prewarm)
    monkeypatch.setattr(prewarm_command, "set_console_log_level", lambda _level: None)

    assert prewarm_command.main(arguments) == 0
    assert [getattr(store, "mode", None) for store in factory_store] == [expected_mode]
    if expected_mode == "read-only":
        assert opened == [
            ("read-only-enter", database_path),
            ("read-only-exit", database_path),
        ]
    elif expected_mode == "writable":
        assert opened == [("writable", database_path)]
    else:
        assert opened == []
        assert not database_path.parent.exists()
