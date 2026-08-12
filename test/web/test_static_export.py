# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import builtins
import json
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

import codenib.web.local as local_module
import codenib.web.static_export as static_module
from codenib.artifacts.security import assert_publishable_tree
from codenib.compiler.artifact_fingerprints import bm25_artifact_file_fingerprints
from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.graph.code_graph import CodeGraph
from codenib.source_fingerprint import (
    RepositoryChangedError,
    RepositorySourceBinding,
    fingerprint_repository,
)
from codenib.web.config import REGISTRY_FILENAME, RepoEntry, load_registry
from codenib.web.repo_registry import RepoBundle
from codenib.web.static_export import (
    _GRAPH_SOURCE_MAX_ANCHORS,
    _GRAPH_SOURCE_PAGE_MAX_BYTES,
    STATIC_EXPORT_MANIFEST,
    _embed_page_graph_sources,
    _load_static_bundle,
    export_static_wiki,
    normalize_base_path,
)
from codenib.wiki.builder import WikiBuilder


class _Builder:
    def __init__(self, _bundle, *, source_reader=None) -> None:
        self.source_reader = source_reader
        self.pages = {
            "overview": {
                "id": "overview",
                "title": "Overview",
                "markdown": "# Overview\n\nThe runtime is source linked.",
                "citations": [
                    {
                        "file": "src/runtime.py",
                        "start_line": 1,
                        "end_line": 2,
                        "node_name": "run",
                        "type": "function",
                        "score": None,
                        "content": None,
                    }
                ],
                "diagram": "",
            },
            "architecture": {
                "id": "architecture",
                "title": "Architecture",
                "markdown": "# Architecture",
                "citations": [],
                "diagram": "",
            },
        }

    def page_tree(self):
        return [
            {"id": "overview", "title": "Overview", "children": []},
            {"id": "architecture", "title": "Architecture", "children": []},
        ]

    def page(self, page_id):
        return self.pages.get(page_id)

    def source(self, file, start, end):
        assert file == "src/runtime.py"
        lines = ["def run():\n", "    return 'ready'\n"]
        first = max(1, start or 1)
        last = max(first, end or len(lines))
        content = "".join(lines[first - 1 : last])
        return {
            "file": file,
            "start_line": first,
            "end_line": first + len(content.splitlines()) - 1,
            "content": content,
        }


def _frontend(root: Path) -> Path:
    frontend = root / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text(
        "<!doctype html><html><head><base href='/'></head><body>"
        "<script src='./runtime-config.js'></script>"
        "<script type='module' src='./assets/app.js'></script>"
        "<a href='?p=module'>Module</a><a href='#heading'>Heading</a>"
        "</body></html>",
        encoding="utf-8",
    )
    (frontend / "runtime-config.js").write_text(
        'window.__CODENIB_API_BASE__ = "";\n', encoding="utf-8"
    )
    (frontend / "assets" / "app.js").write_text(
        "console.log('wiki');\n", encoding="utf-8"
    )
    return frontend


def _manifest(repo: Path, artifact: Path) -> tuple[RepoManifest, Path]:
    repo.mkdir(parents=True, exist_ok=True)
    source = fingerprint_repository(repo)
    entry = IndexEntry(
        index_type="bm25",
        path=str(artifact / "bm25"),
        built_at="2026-08-04T00:00:00Z",
        built_at_epoch=0.0,
        status="fresh",
        commit="abc123",
        source_fingerprint=source.value,
    )
    manifest = RepoManifest(
        repo_path=str(repo),
        commit="abc123",
        source_fingerprint=source.value,
        languages=["python"],
        file_count=source.file_count,
        indexes={"bm25": entry},
        capabilities={"sparse_search": True},
    )
    path = artifact / "repo_manifest.json"
    manifest.save(path)
    return manifest, path


@pytest.fixture
def export_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "runtime.py").write_text(
        "def run():\n    return 'ready'\n", encoding="utf-8"
    )
    artifact = tmp_path / "artifact"
    manifest, manifest_path = _manifest(repo, artifact)
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            instance_id="demo",
            repo="owner/demo",
            repo_dir=str(repo),
        ),
        manifest=manifest,
        info=lambda: {
            "id": "demo",
            "name": "owner/demo @ abc123",
            "repo": "owner/demo",
            "base_commit": "abc123",
            "commit_short": "abc123",
            "language": "python",
            "description": "A demo repository.",
            "problem_statement": "private benchmark text",
            "languages": ["python"],
            "file_count": 1,
            "capabilities": {"sparse_search": True, "chat": True},
            "graph_coverage": None,
        },
        code_graph=lambda: None,
        hierarchical_graph=lambda: None,
    )
    local = SimpleNamespace(
        repo_id="demo",
        data_dir=artifact / "wiki",
        config_path=artifact / "wiki" / "config.yaml",
    )

    def prepare_static(_repo, _manifest, **kwargs):
        assert kwargs["repository_slug"] == repo.name
        return local

    monkeypatch.setattr(
        "codenib.web.static_export.prepare_local_wiki",
        prepare_static,
    )
    monkeypatch.setattr(
        "codenib.web.static_export._load_static_bundle",
        lambda _local, _manifest_path, **_kwargs: bundle,
    )
    monkeypatch.setattr("codenib.wiki.WikiBuilder", _Builder)

    return SimpleNamespace(
        repo=repo,
        artifact=artifact,
        manifest_path=manifest_path,
        frontend=_frontend(tmp_path),
        output=tmp_path / "site",
    )


def test_explicit_static_slug_ignores_ambient_git_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "trusted-local"
    repo.mkdir()
    (repo / "source.py").write_text("value = 1\n", encoding="utf-8")
    _manifest_value, manifest_path = _manifest(repo, tmp_path / "artifact")
    origin_calls: list[Path] = []

    def ambient_origin(path: Path) -> str:
        origin_calls.append(path)
        return "https://github.com/attacker/beta.git"

    monkeypatch.setattr(local_module, "_origin_url", ambient_origin)

    prepared = local_module.prepare_local_wiki(
        repo,
        manifest_path,
        frontend_port=0,
        repository_slug=repo.name,
    )

    entries = load_registry(str(prepared.data_dir / REGISTRY_FILENAME))
    assert origin_calls == []
    assert prepared.repo_id == "trusted-local"
    assert len(entries) == 1
    assert entries[0].repo == "trusted-local"


def test_explicit_static_slug_rejects_surrogateescape_as_validation_error(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="valid UTF-8"):
        local_module.prepare_local_wiki(
            tmp_path,
            tmp_path / "missing-manifest.json",
            frontend_port=0,
            repository_slug="repo-\udcff",
        )


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _bound_source_export_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    repo = tmp_path / "repo"
    runtime = repo / "src" / "runtime.py"
    runtime.parent.mkdir(parents=True)
    runtime.write_text(
        "def run():\n    return 'trusted-source'\n",
        encoding="utf-8",
    )
    readme = repo / "README.md"
    readme.write_text(
        "# Bound Demo\n\n"
        "Bound Demo provides exact repository evidence for source exploration.\n",
        encoding="utf-8",
    )
    artifact = tmp_path / "artifact"
    manifest, manifest_path = _manifest(repo, artifact)
    local = SimpleNamespace(
        repo_id="demo",
        data_dir=artifact / "wiki",
        config_path=artifact / "wiki" / "config.yaml",
    )
    graph = CodeGraph()
    graph._add_vertex(
        "src/runtime.py:run()",
        {
            "type": "function",
            "file": "src/runtime.py",
            "start_line": 0,
            "end_line": 1,
            "unified_name": "src/runtime.py:run()",
        },
    )

    def load_bundle(_local, _manifest_path, *, source_reader):
        entry = RepoEntry(
            instance_id="demo",
            repo="owner/demo",
            base_commit=manifest.commit,
            language="python",
            repo_dir=str(repo),
            manifest_path=str(manifest_path),
        )
        document = SimpleNamespace(
            page_content="def run():\n    return 'stale-index-content'\n",
            metadata={
                "file": str(runtime),
                "name": "run",
                "chunk_type": "function",
                "start_line": 0,
                "end_line": 1,
            },
        )
        bundle = RepoBundle(
            entry=entry,
            manifest=manifest,
            bm25=SimpleNamespace(documents=[document]),
            source_reader=source_reader,
        )
        bundle._code_graph_loaded = True
        bundle._code_graph = graph
        return bundle

    monkeypatch.setattr(static_module, "prepare_local_wiki", lambda *_a, **_k: local)
    monkeypatch.setattr(static_module, "_load_static_bundle", load_bundle)
    return SimpleNamespace(
        repo=repo,
        runtime=runtime,
        readme=readme,
        manifest_path=manifest_path,
        frontend=_frontend(tmp_path),
        output=tmp_path / "site",
    )


def test_static_bundle_rejects_bm25_that_no_longer_matches_manifest(tmp_path):
    artifact = tmp_path / "artifact"
    bm25_dir = artifact / "bm25"
    bm25_dir.mkdir(parents=True)
    documents = bm25_dir / "documents.json"
    documents.write_text('[{"content":"alpha"}]')
    (bm25_dir / "bm25_metadata.json").write_text('{"max_k":10}')
    _, manifest_path = _manifest(tmp_path / "repo", artifact)
    manifest = RepoManifest.load(manifest_path)
    manifest.indexes["bm25"].config["artifact_file_fingerprints"] = (
        bm25_artifact_file_fingerprints(bm25_dir)
    )
    manifest.save(manifest_path)
    documents.write_text(documents.read_text().replace("alpha", "omega"))
    local = SimpleNamespace(data_dir=artifact / "wiki", repo_id="demo")

    with pytest.raises(ValueError, match="manifest fingerprints"):
        _load_static_bundle(local, manifest_path)


def test_static_export_is_deterministic_and_publishable(
    export_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = export_setup
    first = export_static_wiki(
        setup.repo,
        setup.manifest_path,
        setup.output,
        frontend_dir=setup.frontend,
        base_path="/demo",
        environ={"OPENAI_API_KEY": "not-present-in-output"},
    )
    first_bytes = _tree_bytes(setup.output)

    second = export_static_wiki(
        setup.repo,
        setup.manifest_path,
        setup.output,
        frontend_dir=setup.frontend,
        base_path="/demo",
        environ={"OPENAI_API_KEY": "not-present-in-output"},
    )

    assert first.page_count == second.page_count == 2
    assert _tree_bytes(setup.output) == first_bytes
    manifest = json.loads((setup.output / STATIC_EXPORT_MANIFEST).read_text())
    assert manifest["schema_version"] == "1.0"
    assert manifest["repository"]["commit"] == "abc123"
    assert manifest["repository"]["url"] is None
    assert manifest["source_locations"]["line_base"] == 1
    assert manifest["capabilities"]["static_wiki"] is True
    assert manifest["capabilities"]["chat"] is False
    assert manifest["capabilities"]["codemap"] is False
    assert manifest["capabilities"]["sparse_search"] is False
    assert manifest["views"]["bm25"]["current"] is True

    repos = json.loads((setup.output / "data" / "repos.json").read_text())
    assert repos[0]["problem_statement"] == ""
    assert repos[0]["source_url"] is None
    page = json.loads(
        (
            setup.output / "data" / "repos" / "demo" / "pages" / "overview.json"
        ).read_text()
    )
    assert page["citations"][0]["content"].startswith("def run")
    assert page["generation"]["mode"] == "offline"
    assert b"not-present-in-output" not in b"".join(first_bytes.values())
    assert str(setup.repo).encode() not in b"".join(first_bytes.values())
    runtime = (setup.output / "runtime-config.js").read_text()
    assert '"basePath":"/demo"' in runtime
    assert '"mode":"static"' in runtime
    index = (setup.output / "index.html").read_text()
    assert "<base" not in index
    assert "src='/demo/runtime-config.js'" in index
    assert "src='/demo/assets/app.js'" in index
    assert "href='?p=module'" in index
    assert "href='#heading'" in index


def test_static_export_reads_summary_excerpts_graph_and_paths_from_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _bound_source_export_setup(tmp_path, monkeypatch)
    real_read_prefix = RepositorySourceBinding.read_prefix
    real_read_line_range = RepositorySourceBinding.read_line_range
    prefix_reads: list[str] = []
    line_reads: list[str] = []

    def counted_read_prefix(self, relative, *, max_bytes):
        prefix_reads.append(relative)
        return real_read_prefix(self, relative, max_bytes=max_bytes)

    def counted_read_line_range(
        self,
        relative,
        *,
        start_line,
        end_line,
        max_bytes,
    ):
        line_reads.append(relative)
        return real_read_line_range(
            self,
            relative,
            start_line=start_line,
            end_line=end_line,
            max_bytes=max_bytes,
        )

    real_open = builtins.open

    def reject_ambient_source_open(file, *args, **kwargs):
        try:
            candidate = Path(file)
        except TypeError:
            return real_open(file, *args, **kwargs)
        if candidate == setup.repo or setup.repo in candidate.parents:
            raise AssertionError(
                f"ambient repository read escaped binding: {candidate}"
            )
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(
        RepositorySourceBinding,
        "read_prefix",
        counted_read_prefix,
    )
    monkeypatch.setattr(
        RepositorySourceBinding,
        "read_line_range",
        counted_read_line_range,
    )
    monkeypatch.setattr(builtins, "open", reject_ambient_source_open)

    export_static_wiki(
        setup.repo,
        setup.manifest_path,
        setup.output,
        frontend_dir=setup.frontend,
    )

    repos = json.loads((setup.output / "data" / "repos.json").read_text())
    assert repos[0]["description"].startswith("Bound Demo provides exact")
    page = json.loads(
        (setup.output / "data/repos/demo/pages/overview.json").read_text(
            encoding="utf-8"
        )
    )
    assert page["citations"][0]["file"] == "src/runtime.py"
    assert "trusted-source" in page["citations"][0]["content"]
    assert "stale-index-content" not in page["citations"][0]["content"]
    graph = json.loads(
        (setup.output / "data/repos/demo/page-graphs/overview.json").read_text(
            encoding="utf-8"
        )
    )
    assert "trusted-source" in graph["nodes"][0]["source"]["content"]
    assert "README.md" in prefix_reads
    assert "src/runtime.py" in prefix_reads  # generated-source header check
    assert line_reads.count("src/runtime.py") >= 2


def test_static_export_rejects_summary_source_drift_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _bound_source_export_setup(tmp_path, monkeypatch)
    real_read_prefix = RepositorySourceBinding.read_prefix
    mutated = False

    def mutate_summary_before_read(self, relative, *, max_bytes):
        nonlocal mutated
        if relative == "README.md" and not mutated:
            mutated = True
            setup.readme.write_text(
                "# Drifted\n\nThis summary was changed after source binding.\n",
                encoding="utf-8",
            )
        return real_read_prefix(self, relative, max_bytes=max_bytes)

    monkeypatch.setattr(
        RepositorySourceBinding,
        "read_prefix",
        mutate_summary_before_read,
    )

    with pytest.raises(RepositoryChangedError):
        export_static_wiki(
            setup.repo,
            setup.manifest_path,
            setup.output,
            frontend_dir=setup.frontend,
        )

    assert mutated
    assert not setup.output.exists()


def test_static_export_rejects_graph_excerpt_drift_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _bound_source_export_setup(tmp_path, monkeypatch)
    real_source = WikiBuilder.source
    mutated = False

    def mutate_before_graph_source(self, file, start=None, end=None):
        nonlocal mutated
        if not mutated:
            mutated = True
            setup.runtime.write_text(
                "def run():\n    return 'graph-drift'\n",
                encoding="utf-8",
            )
        return real_source(self, file, start, end)

    monkeypatch.setattr(WikiBuilder, "source", mutate_before_graph_source)

    with pytest.raises(RepositoryChangedError):
        export_static_wiki(
            setup.repo,
            setup.manifest_path,
            setup.output,
            frontend_dir=setup.frontend,
        )

    assert mutated
    assert not setup.output.exists()


def test_static_export_rejects_index_path_outside_bound_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _bound_source_export_setup(tmp_path, monkeypatch)
    real_captured_path = RepositorySourceBinding.captured_relative_path

    def omit_runtime(binding, path):
        if str(path).replace("\\", "/").endswith("src/runtime.py"):
            return None
        return real_captured_path(binding, path)

    monkeypatch.setattr(
        RepositorySourceBinding,
        "captured_relative_path",
        omit_runtime,
    )

    with pytest.raises(ValueError, match="absent from the authenticated repository"):
        export_static_wiki(
            setup.repo,
            setup.manifest_path,
            setup.output,
            frontend_dir=setup.frontend,
        )

    assert not setup.output.exists()


def test_static_export_rejects_source_root_swap_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _bound_source_export_setup(tmp_path, monkeypatch)
    real_copy = static_module._copy_frontend
    swapped = False

    def swap_source_root(*args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            saved = setup.repo.with_name("saved-repo")
            setup.repo.rename(saved)
            setup.runtime.parent.mkdir(parents=True)
            setup.runtime.write_text(
                "def run():\n    return 'replacement-root'\n",
                encoding="utf-8",
            )
            setup.readme.write_text(
                "# Replacement\n\nReplacement repository source tree.\n",
                encoding="utf-8",
            )
        return real_copy(*args, **kwargs)

    monkeypatch.setattr(static_module, "_copy_frontend", swap_source_root)

    with pytest.raises(RepositoryChangedError):
        export_static_wiki(
            setup.repo,
            setup.manifest_path,
            setup.output,
            frontend_dir=setup.frontend,
        )

    assert swapped
    assert not setup.output.exists()


def test_static_export_preserves_output_changed_during_build(
    export_setup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = export_setup
    export_static_wiki(
        setup.repo,
        setup.manifest_path,
        setup.output,
        frontend_dir=setup.frontend,
    )
    previous = _tree_bytes(setup.output)

    from codenib.web import static_export as static_export_module

    real_copy = static_export_module._copy_frontend
    mutated = False

    def mutate_old_output(*args, **kwargs):
        nonlocal mutated
        if not mutated:
            mutated = True
            nested = setup.output / "late"
            nested.mkdir()
            (nested / "preserve.txt").write_text("preserve", encoding="utf-8")
        return real_copy(*args, **kwargs)

    monkeypatch.setattr(static_export_module, "_copy_frontend", mutate_old_output)

    with pytest.raises(RuntimeError, match="changed before directory publication"):
        export_static_wiki(
            setup.repo,
            setup.manifest_path,
            setup.output,
            frontend_dir=setup.frontend,
        )

    for relative, content in previous.items():
        assert (setup.output / relative).read_bytes() == content
    assert (setup.output / "late" / "preserve.txt").read_text(encoding="utf-8") == (
        "preserve"
    )


def test_static_export_rejects_output_symlink(
    export_setup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = export_setup
    victim = setup.output
    export_static_wiki(
        setup.repo,
        setup.manifest_path,
        victim,
        frontend_dir=setup.frontend,
    )
    previous = _tree_bytes(victim)
    output = setup.output.parent / "output-link"
    output.symlink_to(victim, target_is_directory=True)

    with pytest.raises(ValueError, match="not a directory or is a link"):
        export_static_wiki(
            setup.repo,
            setup.manifest_path,
            output,
            frontend_dir=setup.frontend,
        )

    assert output.is_symlink()
    assert _tree_bytes(victim) == previous


def test_static_export_preserves_primary_when_stage_discard_fails(
    export_setup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = KeyboardInterrupt("static-build-primary")
    secondary = SystemExit("static-discard-secondary")

    def fail_copy(*_args, **_kwargs):
        raise primary

    def fail_discard(_stage):
        raise secondary

    monkeypatch.setattr(static_module, "_copy_frontend", fail_copy)
    monkeypatch.setattr(static_module.OwnedDirectoryStage, "discard", fail_discard)

    with pytest.raises(KeyboardInterrupt, match="static-build-primary") as caught:
        export_static_wiki(
            export_setup.repo,
            export_setup.manifest_path,
            export_setup.output,
            frontend_dir=export_setup.frontend,
        )

    assert caught.value is primary
    notes = tuple(getattr(primary, "__notes__", ())) + tuple(
        getattr(primary, "_codenib_cleanup_notes", ())
    )
    assert any(
        "static export stage discard also failed" in note
        and "static-discard-secondary" in note
        for note in notes
    )


def test_static_export_preserves_cancellation_and_retains_failed_source_cleanup(
    export_setup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = KeyboardInterrupt("static-build-cancelled")
    cleanup = SystemExit("static-source-close-cancelled")
    real_close = RepositorySourceBinding.close

    def cancel_copy(*_args, **_kwargs):
        raise primary

    def fail_before_close(_binding):
        raise cleanup

    monkeypatch.setattr(static_module, "_copy_frontend", cancel_copy)
    monkeypatch.setattr(RepositorySourceBinding, "close", fail_before_close)

    with pytest.raises(KeyboardInterrupt, match="static-build-cancelled") as caught:
        export_static_wiki(
            export_setup.repo,
            export_setup.manifest_path,
            export_setup.output,
            frontend_dir=export_setup.frontend,
        )

    assert caught.value is primary
    assert caught.value.__cause__ is cleanup
    assert not export_setup.output.exists()
    cleanup_owner = caught.value.source_cleanup_owner
    assert not cleanup_owner.closed

    monkeypatch.setattr(RepositorySourceBinding, "close", real_close)
    cleanup_owner.close()
    assert cleanup_owner.closed


def test_static_export_close_after_cancellation_still_prevents_publication(
    export_setup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = SystemExit("static-source-closed-then-cancelled")
    real_close = RepositorySourceBinding.close
    close_calls = 0

    def close_then_cancel(binding):
        nonlocal close_calls
        close_calls += 1
        real_close(binding)
        raise cancellation

    monkeypatch.setattr(RepositorySourceBinding, "close", close_then_cancel)

    with pytest.raises(SystemExit, match="closed-then-cancelled") as caught:
        export_static_wiki(
            export_setup.repo,
            export_setup.manifest_path,
            export_setup.output,
            frontend_dir=export_setup.frontend,
        )

    assert caught.value is cancellation
    assert close_calls == 1
    assert getattr(caught.value, "source_cleanup_owner", None) is None
    assert not export_setup.output.exists()


def test_static_export_never_overwrites_raced_runtime_config(
    export_setup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = export_setup
    real_write = static_module.OwnedDirectoryStage.write_file
    raced = False

    def race_runtime_config(self, relative, chunks, **kwargs):
        nonlocal raced
        if str(relative) == "runtime-config.js" and not raced:
            raced = True
            (self.path / "runtime-config.js").write_bytes(b"foreign-config")
        return real_write(self, relative, chunks, **kwargs)

    monkeypatch.setattr(
        static_module.OwnedDirectoryStage,
        "write_file",
        race_runtime_config,
    )

    with pytest.raises((RuntimeError, ValueError)):
        export_static_wiki(
            setup.repo,
            setup.manifest_path,
            setup.output,
            frontend_dir=setup.frontend,
        )

    assert raced
    assert not setup.output.exists()
    assert any(
        path.read_bytes() == b"foreign-config"
        for path in setup.output.parent.rglob("runtime-config.js")
    )


def test_static_export_rejects_reversible_nested_directory_replacement(
    export_setup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = export_setup
    foreign = setup.output.parent / "foreign-data"
    foreign.mkdir()
    valuable = foreign / "valuable.txt"
    valuable.write_text("keep", encoding="utf-8")
    real_stat = static_module.os.stat
    real_write_json = static_module._write_json
    armed = False
    swapped = False

    def arm_nested_swap(stage, relative, value):
        nonlocal armed
        if relative.endswith("/wiki.json"):
            armed = True
        return real_write_json(stage, relative, value)

    def reversible_stat(path, *, dir_fd=None, follow_symlinks=True):
        nonlocal swapped
        if armed and not swapped and path == "data" and dir_fd is not None:
            swapped = True
            static_module.os.rename(
                "data",
                ".saved-data",
                src_dir_fd=dir_fd,
                dst_dir_fd=dir_fd,
            )
            static_module.os.rename(foreign, "data", dst_dir_fd=dir_fd)
            try:
                return real_stat(
                    path,
                    dir_fd=dir_fd,
                    follow_symlinks=follow_symlinks,
                )
            finally:
                static_module.os.rename("data", foreign, src_dir_fd=dir_fd)
                static_module.os.rename(
                    ".saved-data",
                    "data",
                    src_dir_fd=dir_fd,
                    dst_dir_fd=dir_fd,
                )
        return real_stat(
            path,
            dir_fd=dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(static_module, "_write_json", arm_nested_swap)
    monkeypatch.setattr(static_module.os, "stat", reversible_stat)

    with pytest.raises(ValueError, match="component changed"):
        export_static_wiki(
            setup.repo,
            setup.manifest_path,
            setup.output,
            frontend_dir=setup.frontend,
        )

    assert swapped
    assert valuable.read_text(encoding="utf-8") == "keep"
    assert not setup.output.exists()


def test_publishability_rejects_json_escaped_windows_path(tmp_path: Path) -> None:
    root = tmp_path / "site"
    root.mkdir()
    windows_path = PureWindowsPath(r"C:\build\repository")
    (root / "page.json").write_text(
        json.dumps({"source": str(windows_path)}),
        encoding="utf-8",
    )

    class ResolvedWindowsPath:
        def expanduser(self):
            return self

        def resolve(self):
            return windows_path

    with pytest.raises(ValueError, match="absolute build-machine path"):
        assert_publishable_tree(
            root,
            forbidden_paths=(ResolvedWindowsPath(),),
            environ={},
            label="static export",
        )


def test_static_export_rejects_a_configured_secret(
    export_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "provider-secret-value"

    class SecretBuilder(_Builder):
        def __init__(self, bundle, *, source_reader=None) -> None:
            super().__init__(bundle, source_reader=source_reader)
            self.pages["overview"]["markdown"] = f"# Overview\n\n{secret}"

    monkeypatch.setattr("codenib.wiki.WikiBuilder", SecretBuilder)

    with pytest.raises(ValueError, match="configured credential"):
        export_static_wiki(
            export_setup.repo,
            export_setup.manifest_path,
            export_setup.output,
            frontend_dir=export_setup.frontend,
            environ={"OPENAI_API_KEY": secret},
        )
    assert not export_setup.output.exists()


def test_static_export_reader_rejects_late_decoded_secret(
    export_setup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "pässwörd-secret"
    original_write_json = static_module._write_json

    def write_decoded_secret(stage, relative, value):
        if relative.endswith("/pages/overview.json"):
            value = {"value": secret}
        return original_write_json(stage, relative, value)

    monkeypatch.setattr(
        static_module,
        "_write_json",
        write_decoded_secret,
    )
    with pytest.raises(ValueError, match="configured credential"):
        export_static_wiki(
            export_setup.repo,
            export_setup.manifest_path,
            export_setup.output,
            frontend_dir=export_setup.frontend,
            environ={"OPENAI_API_KEY": secret},
        )
    assert not export_setup.output.exists()


def test_static_export_advertises_only_precomputed_page_graphs(
    export_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "codenib.web.static_export._page_graph",
        lambda _bundle, _page: {
            "available": True,
            "nodes": [
                {
                    "id": "run",
                    "file": "src/runtime.py",
                    "line": 1,
                    "end_line": 2,
                }
            ],
            "hierarchy": {
                "root": "root",
                "nodes": [
                    {
                        "id": "symbol-run",
                        "kind": "symbol",
                        "node_id": "run",
                        "file": "src/runtime.py",
                        "line": 1,
                        "end_line": 2,
                    },
                    {
                        "id": "symbol-scope",
                        "kind": "symbol",
                        "node_id": "src/runtime.py:Runtime",
                        "file": "src/runtime.py",
                        "line": 1,
                        "end_line": 2,
                    },
                ],
            },
            "edges": [
                {
                    "source": "run",
                    "target": "run",
                    "anchors": [{"file": "src/runtime.py", "line": 2}],
                }
            ],
            "mermaid": "",
        },
    )

    export_static_wiki(
        export_setup.repo,
        export_setup.manifest_path,
        export_setup.output,
        frontend_dir=export_setup.frontend,
    )

    repos = json.loads((export_setup.output / "data" / "repos.json").read_text())
    assert repos[0]["capabilities"]["wiki_graph"] is True
    assert repos[0]["capabilities"]["codemap"] is False
    graph = json.loads(
        (
            export_setup.output
            / "data"
            / "repos"
            / "demo"
            / "page-graphs"
            / "overview.json"
        ).read_text()
    )
    assert graph["available"] is True
    assert graph["nodes"][0]["source"]["content"].startswith("def run")
    assert "source" not in graph["hierarchy"]["nodes"][0]
    assert graph["hierarchy"]["nodes"][1]["source"]["content"].startswith("def run")
    assert graph["edges"][0]["anchors"][0]["source"]["content"].endswith(
        "return 'ready'\n"
    )


def test_page_graph_source_previews_are_bounded_cached_and_path_safe() -> None:
    class SourceBuilder:
        def __init__(self) -> None:
            self.calls = []

        def source(self, file, start, end):
            self.calls.append((file, start, end))
            return {
                "file": file,
                "start_line": start,
                "end_line": end,
                "content": "".join(
                    f"line {line} {'x' * 2_000}\n" for line in range(start, end + 1)
                ),
            }

    builder = SourceBuilder()
    graph = {
        "available": True,
        "nodes": [
            {
                "id": "wide",
                "file": "src/runtime.py",
                "line": 10,
                "end_line": 10_000,
            },
            {
                "id": "unsafe",
                "file": "../secret.py",
                "line": 1,
                "end_line": 2,
            },
        ],
        "hierarchy": {
            "nodes": [
                {
                    "id": "mapped",
                    "kind": "symbol",
                    "node_id": "wide",
                    "file": "src/runtime.py",
                    "line": 10,
                    "end_line": 10_000,
                },
                {
                    "id": "scope",
                    "kind": "symbol",
                    "node_id": "canonical-scope",
                    "file": "src/runtime.py",
                    "line": 30,
                    "end_line": 35,
                },
            ]
        },
        "edges": [
            {
                "anchors": [
                    {"file": "src/runtime.py", "line": 30},
                    {"file": "src/runtime.py", "line": 30},
                    {"file": "/tmp/secret.py", "line": 1},
                    *[
                        {"file": "src/runtime.py", "line": 100 + index * 20}
                        for index in range(100)
                    ],
                ]
            }
        ],
    }

    enriched = _embed_page_graph_sources(builder, graph)

    wide = enriched["nodes"][0]["source"]
    assert len(wide["content"].encode("utf-8")) <= 64 * 1024
    assert len(wide["content"].splitlines()) <= 160
    assert builder.calls[0] == ("src/runtime.py", 7, 166)
    assert enriched["nodes"][1]["source"] is None
    hierarchy = enriched["hierarchy"]["nodes"]
    assert "source" not in hierarchy[0]
    assert "source" in hierarchy[1]
    anchors = enriched["edges"][0]["anchors"]
    assert anchors[0]["source"] == anchors[1]["source"]
    assert anchors[2]["source"] is None
    assert builder.calls.count(("src/runtime.py", 24, 36)) == 1
    attached_anchors = [
        anchor for anchor in anchors if anchor.get("source") is not None
    ]
    assert len(attached_anchors) < 50  # the per-page byte budget stops first
    assert any(
        anchor.get("source") is None
        for anchor in anchors
        if anchor.get("file") == "src/runtime.py"
    )
    assert len(builder.calls) <= _GRAPH_SOURCE_MAX_ANCHORS + 2
    attached_sources = [
        value["source"]
        for value in [*enriched["nodes"], *hierarchy, *anchors]
        if value.get("source") is not None
    ]
    assert (
        sum(len(json.dumps(source).encode("utf-8")) for source in attached_sources)
        <= _GRAPH_SOURCE_PAGE_MAX_BYTES
    )


def test_page_graph_source_preview_rejects_mismatched_returned_range() -> None:
    class MismatchedBuilder:
        def source(self, file, start, end):
            return {
                "file": file,
                "start_line": start + 1,
                "end_line": end + 1,
                "content": "wrong range\n",
            }

    graph = {
        "nodes": [
            {
                "id": "run",
                "file": "src/runtime.py",
                "line": 10,
                "end_line": 12,
            }
        ],
        "hierarchy": {"nodes": []},
        "edges": [],
    }

    _embed_page_graph_sources(MismatchedBuilder(), graph)

    assert graph["nodes"][0]["source"] is None


def test_invalid_anchors_do_not_exhaust_source_preview_quota() -> None:
    class SourceBuilder:
        def source(self, file, start, end):
            return {
                "file": file,
                "start_line": start,
                "end_line": end,
                "content": "source\n",
            }

    graph = {
        "nodes": [],
        "hierarchy": {"nodes": []},
        "edges": [
            {
                "anchors": [
                    *[
                        {"file": "../outside.py", "line": index + 1}
                        for index in range(_GRAPH_SOURCE_MAX_ANCHORS)
                    ],
                    {"file": "src/runtime.py", "line": 10},
                ]
            }
        ],
    }

    _embed_page_graph_sources(SourceBuilder(), graph)

    assert all(anchor["source"] is None for anchor in graph["edges"][0]["anchors"][:-1])
    assert "source" in graph["edges"][0]["anchors"][-1]


def test_page_graph_source_preview_keeps_the_highlighted_line_when_trimmed() -> None:
    class SourceBuilder:
        def source(self, file, start, end):
            lines = []
            for line in range(start, end + 1):
                if line == 8:
                    lines.append("generated = '" + ("x" * (70 * 1024)) + "'\n")
                elif line == 10:
                    lines.append("TARGET = build_runtime()\n")
                else:
                    lines.append(f"line_{line} = {line}\n")
            return {
                "file": file,
                "start_line": start,
                "end_line": end,
                "content": "".join(lines),
            }

    graph = {
        "nodes": [
            {
                "id": "runtime",
                "file": "src/runtime.py",
                "line": 10,
                "end_line": 12,
            }
        ],
        "hierarchy": {"nodes": []},
        "edges": [],
    }

    _embed_page_graph_sources(SourceBuilder(), graph)

    source = graph["nodes"][0]["source"]
    assert source is not None
    assert source["start_line"] <= 10 <= source["end_line"]
    assert "TARGET = build_runtime()" in source["content"]
    assert len(source["content"].encode("utf-8")) <= 64 * 1024


def test_static_export_does_not_replace_an_unrelated_directory(export_setup) -> None:
    export_setup.output.mkdir()
    (export_setup.output / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="refusing to replace"):
        export_static_wiki(
            export_setup.repo,
            export_setup.manifest_path,
            export_setup.output,
            frontend_dir=export_setup.frontend,
        )

    assert (export_setup.output / "keep.txt").read_text() == "keep"


def test_static_export_rejects_index_root_overlap(export_setup) -> None:
    with pytest.raises(ValueError, match="outside the index root"):
        export_static_wiki(
            export_setup.repo,
            export_setup.manifest_path,
            export_setup.manifest_path.parent,
            frontend_dir=export_setup.frontend,
        )


def test_static_export_rejects_manifest_directory_alias_overlap(
    export_setup,
) -> None:
    setup = export_setup
    before = _tree_bytes(setup.artifact)
    manifest_root_alias = setup.artifact.with_name("artifact-alias")
    manifest_root_alias.symlink_to(setup.artifact, target_is_directory=True)
    aliased_manifest = manifest_root_alias / setup.manifest_path.name
    output = setup.artifact / "directory-alias-site"

    with pytest.raises(ValueError, match="outside the index root"):
        export_static_wiki(
            setup.repo,
            aliased_manifest,
            output,
            frontend_dir=setup.frontend,
        )

    assert manifest_root_alias.is_symlink()
    assert not output.exists()
    assert _tree_bytes(setup.artifact) == before


def test_static_export_rejects_manifest_file_alias_overlap(export_setup) -> None:
    setup = export_setup
    before = _tree_bytes(setup.artifact)
    manifest_alias = setup.artifact.parent / "manifest-alias.json"
    manifest_alias.symlink_to(setup.manifest_path)
    output = setup.artifact / "file-alias-site"

    with pytest.raises(ValueError, match="outside the index root"):
        export_static_wiki(
            setup.repo,
            manifest_alias,
            output,
            frontend_dir=setup.frontend,
        )

    assert manifest_alias.is_symlink()
    assert not output.exists()
    assert _tree_bytes(setup.artifact) == before


def test_static_export_rejects_absolute_citation_paths(
    export_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    class AbsolutePathBuilder(_Builder):
        def __init__(self, bundle, *, source_reader=None) -> None:
            super().__init__(bundle, source_reader=source_reader)
            self.pages["overview"]["citations"][0]["file"] = "/tmp/source.py"

    monkeypatch.setattr("codenib.wiki.WikiBuilder", AbsolutePathBuilder)

    with pytest.raises(ValueError, match="repository-relative"):
        export_static_wiki(
            export_setup.repo,
            export_setup.manifest_path,
            export_setup.output,
            frontend_dir=export_setup.frontend,
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [("/", "/"), ("/demo/", "/demo"), ("/org/demo", "/org/demo")],
)
def test_normalize_base_path(value: str, expected: str) -> None:
    assert normalize_base_path(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "demo",
        "https://example.com/demo",
        "/demo?token=x",
        "/demo/../other",
        "/demo%22onload",
        "/demo%3Fquery",
        "/demo%23fragment",
        '/demo" onload="alert(1)',
    ],
)
def test_normalize_base_path_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_base_path(value)
