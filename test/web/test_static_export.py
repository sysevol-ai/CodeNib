# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

from codenib.artifacts.security import assert_publishable_tree
from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.web.static_export import (
    STATIC_EXPORT_MANIFEST,
    export_static_wiki,
    normalize_base_path,
)


class _Builder:
    def __init__(self, _bundle) -> None:
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
        assert (file, start, end) == ("src/runtime.py", 1, 2)
        return {
            "file": file,
            "start_line": start,
            "end_line": end,
            "content": "def run():\n    return 'ready'\n",
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
    entry = IndexEntry(
        index_type="bm25",
        path=str(artifact / "bm25"),
        built_at="2026-08-04T00:00:00Z",
        built_at_epoch=0.0,
        status="fresh",
        commit="abc123",
        source_fingerprint="source-1",
    )
    manifest = RepoManifest(
        repo_path=str(repo),
        commit="abc123",
        source_fingerprint="source-1",
        languages=["python"],
        file_count=1,
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
    monkeypatch.setattr(
        "codenib.web.static_export.prepare_local_wiki",
        lambda *_args, **_kwargs: local,
    )
    monkeypatch.setattr(
        "codenib.web.static_export._load_static_bundle",
        lambda _local, _manifest_path: bundle,
    )
    monkeypatch.setattr("codenib.wiki.WikiBuilder", _Builder)

    return SimpleNamespace(
        repo=repo,
        artifact=artifact,
        manifest_path=manifest_path,
        frontend=_frontend(tmp_path),
        output=tmp_path / "site",
    )


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_static_export_is_deterministic_and_publishable(
    export_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = export_setup
    monkeypatch.setattr(
        "codenib.web.static_export._github_repository_url",
        lambda _repo: "https://github.com/owner/demo",
    )
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
    assert manifest["repository"]["url"] == "https://github.com/owner/demo"
    assert manifest["source_locations"]["line_base"] == 1
    assert manifest["capabilities"]["static_wiki"] is True
    assert manifest["capabilities"]["chat"] is False
    assert manifest["capabilities"]["codemap"] is False
    assert manifest["capabilities"]["sparse_search"] is False
    assert manifest["views"]["bm25"]["current"] is True

    repos = json.loads((setup.output / "data" / "repos.json").read_text())
    assert repos[0]["problem_statement"] == ""
    assert repos[0]["source_url"] == "https://github.com/owner/demo"
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
        def __init__(self, bundle) -> None:
            super().__init__(bundle)
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


def test_static_export_advertises_only_precomputed_page_graphs(
    export_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "codenib.web.static_export._page_graph",
        lambda _bundle, _page: {
            "available": True,
            "nodes": [{"id": "run"}],
            "edges": [],
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


def test_static_export_rejects_absolute_citation_paths(
    export_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    class AbsolutePathBuilder(_Builder):
        def __init__(self, bundle) -> None:
            super().__init__(bundle)
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
