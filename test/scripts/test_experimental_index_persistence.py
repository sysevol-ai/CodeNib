# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

from scripts.experimental import index_persistence


def test_experiment_exposes_only_two_source_checkout_commands() -> None:
    parser = index_persistence._parser()
    commands = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )

    assert set(commands.choices) == {"publish-bm25", "export-ref"}


def test_publish_command_forwards_the_ref_revision(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    calls: list[tuple[object, ...]] = []

    class Repository:
        def publish_bm25(self, artifact, *, ref_name, expected_revision):
            calls.append((artifact, ref_name, expected_revision))
            return SimpleNamespace(
                repository="example/project",
                ref_name=ref_name,
                ref_revision=expected_revision + 1,
                snapshot_id="snap_123",
            )

    monkeypatch.setattr(
        index_persistence.IndexRepository,
        "open",
        lambda root: calls.append(("open", root)) or Repository(),
    )

    assert (
        index_persistence.main(
            [
                "publish-bm25",
                str(tmp_path / "artifact"),
                "--store",
                str(tmp_path / "store"),
                "--ref",
                "stable",
                "--expected-revision",
                "4",
            ]
        )
        == 0
    )
    assert calls == [
        ("open", str(tmp_path / "store")),
        (str(tmp_path / "artifact"), "stable", 4),
    ]
    assert "example/project:stable@5 snap_123" in capsys.readouterr().out


def test_export_command_resolves_then_materializes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[tuple[object, ...]] = []
    output = tmp_path / "artifact"

    class Repository:
        def resolve_ref(self, repository, ref_name):
            calls.append(("resolve", repository, ref_name))
            return SimpleNamespace(snapshot=SimpleNamespace(snapshot_id="snap_123"))

        def materialize_snapshot(self, snapshot_id, destination):
            calls.append(("materialize", snapshot_id, destination))
            return SimpleNamespace(root=output)

    monkeypatch.setattr(
        index_persistence.IndexRepository,
        "open",
        lambda root: calls.append(("open", root)) or Repository(),
    )

    assert (
        index_persistence.main(
            [
                "export-ref",
                "example/project",
                str(output),
                "--store",
                str(tmp_path / "store"),
                "--ref",
                "stable",
            ]
        )
        == 0
    )
    assert calls == [
        ("open", str(tmp_path / "store")),
        ("resolve", "example/project", "stable"),
        ("materialize", "snap_123", str(output)),
    ]


def test_command_reports_repository_failures(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    def fail(_root):
        raise RuntimeError("catalog is unavailable")

    monkeypatch.setattr(index_persistence.IndexRepository, "open", fail)

    assert (
        index_persistence.main(
            [
                "publish-bm25",
                str(tmp_path / "artifact"),
                "--store",
                str(tmp_path / "store"),
            ]
        )
        == 1
    )
    assert "error: catalog is unavailable" in capsys.readouterr().err
