# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

import codenib._contained_source as contained_source_module
import codenib.source_fingerprint as source_fingerprint_module
from codenib.code_chunking.base import CodeChunk
from codenib.index.sparse_idx import bm25_index as bm25_module
from codenib.index.sparse_idx.bm25_index import BM25CodeIndexer
from codenib.source_fingerprint import (
    RepositorySourceBinding,
    capture_repository_source,
)
from codenib.types import NODE_TYPE_FILE


class _ZeroIdentityMetadata:
    st_dev = 0
    st_ino = 0

    def __init__(self, wrapped: os.stat_result) -> None:
        self._wrapped = wrapped

    def __getattr__(self, name: str):
        return getattr(self._wrapped, name)


_NON_LF_LINE_BREAKS = [
    pytest.param("\v", id="vertical-tab"),
    pytest.param("\f", id="form-feed"),
    pytest.param("\x85", id="next-line"),
    pytest.param("\u2028", id="line-separator"),
    pytest.param("\u2029", id="paragraph-separator"),
]


def _indexer(
    project_root: Path | None,
    file_path: str,
    *,
    start_line: int = 0,
    end_line: int = 0,
    node_type: str = "function",
) -> BM25CodeIndexer:
    indexer = BM25CodeIndexer(
        chunks=[
            CodeChunk(
                content="def safe_source_marker(): pass",
                start_line=start_line,
                end_line=end_line,
                chunk_type="function",
                name="safe_source_marker",
                file="pkg/source.py",
                node_id="pkg/source.py:safe_source_marker()",
            )
        ],
        project_root=str(project_root) if project_root is not None else None,
    )
    indexer.documents[0].metadata.update(
        {
            "file": file_path,
            "start_line": start_line,
            "end_line": end_line,
            "type": node_type,
        }
    )
    return indexer


def _content(indexer: BM25CodeIndexer, *, wrap_with_ln: bool = False) -> str | None:
    result = indexer.search(
        "safe_source_marker",
        top_k=1,
        return_code_content=True,
        wrap_with_ln=wrap_with_ln,
    )[0]
    return result.content


def _symlink_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")


def test_relative_regular_source_preserves_inclusive_symbol_range(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    source = repo / "pkg" / "source.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"zero\r\none\r\ntwo\r\nthree\r\n")

    indexer = _indexer(
        repo,
        "pkg/source.py",
        start_line=1,
        end_line=2,
    )

    assert _content(indexer) == "one\ntwo\n"
    wrapped = _content(indexer, wrap_with_ln=True)
    assert wrapped is not None
    assert "2 | one" in wrapped
    assert "3 | two" in wrapped


@pytest.mark.parametrize(
    ("source_text", "expected"),
    [
        ("", []),
        ("one", ["one"]),
        ("one\n", ["one\n"]),
        ("one\ntwo", ["one\n", "two"]),
        ("one\n\n", ["one\n", "\n"]),
    ],
)
def test_source_line_splitter_preserves_textio_trailing_newline_shape(
    source_text: str,
    expected: list[str],
) -> None:
    assert bm25_module._split_source_lines(source_text) == expected


@pytest.mark.parametrize("embedded_break", _NON_LF_LINE_BREAKS)
def test_symbol_range_does_not_treat_non_lf_characters_as_lines(
    tmp_path: Path,
    embedded_break: str,
) -> None:
    repo = tmp_path / "repo"
    source = repo / "source.py"
    repo.mkdir()
    first_line = f"prefix{embedded_break}safe_source_marker = 1\n"
    source.write_text(first_line + "second\n", encoding="utf-8")
    indexer = _indexer(repo, "source.py", start_line=0, end_line=0)

    assert _content(indexer) == first_line


def test_regular_file_node_still_returns_the_entire_source(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    source = repo / "source.py"
    repo.mkdir()
    source.write_text("first\nsecond\n", encoding="utf-8")

    indexer = _indexer(repo, "source.py", node_type=NODE_TYPE_FILE)

    assert _content(indexer) == "first\nsecond\n"


def test_bound_search_reads_each_source_file_once_per_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    source = repo / "pkg" / "source.py"
    source.parent.mkdir(parents=True)
    source.write_text("shared_marker_one()\nshared_marker_two()\n", encoding="utf-8")
    indexer = BM25CodeIndexer(
        chunks=[
            CodeChunk(
                content="shared_marker first",
                start_line=0,
                end_line=0,
                chunk_type="function",
                name="shared_marker_one",
                file="pkg/source.py",
                node_id="pkg/source.py:shared_marker_one()",
            ),
            CodeChunk(
                content="shared_marker second",
                start_line=1,
                end_line=1,
                chunk_type="function",
                name="shared_marker_two",
                file="pkg/source.py",
                node_id="pkg/source.py:shared_marker_two()",
            ),
        ],
        project_root=str(repo),
    )
    binding = capture_repository_source(repo)
    indexer.bind_repository_source(binding)

    real_scan = source_fingerprint_module._scan_pinned_repository
    real_read = RepositorySourceBinding._read_record
    scans = 0
    reads = 0

    def counted_scan(*args, **kwargs):
        nonlocal scans
        scans += 1
        return real_scan(*args, **kwargs)

    def counted_read(self, *args, **kwargs):
        nonlocal reads
        reads += 1
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(
        source_fingerprint_module,
        "_scan_pinned_repository",
        counted_scan,
    )
    monkeypatch.setattr(RepositorySourceBinding, "_read_record", counted_read)

    try:
        results = indexer.search(
            "shared_marker",
            top_k=2,
            return_code_content=True,
            wrap_with_ln=False,
        )

        assert len(results) == 2
        assert {result.content for result in results} == {
            "shared_marker_one()\n",
            "shared_marker_two()\n",
        }
        assert reads == 1
        assert scans == 2
    finally:
        binding.close()


def test_bind_repository_source_uses_one_detached_identity_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    indexer = _indexer(None, "source.py")
    binding = capture_repository_source(repo)
    binding_type = type(binding)
    real_snapshot = binding_type.authenticated_identity_snapshot
    substituted_root = tmp_path / "substituted"

    def snapshot_then_replace_public_projection(source):
        snapshot = real_snapshot(source)
        source.root = substituted_root
        source.fingerprint = "sha256-v2:" + ("0" * 64)
        source.file_count = 0
        source.file_records = ()
        return snapshot

    monkeypatch.setattr(
        binding_type,
        "authenticated_identity_snapshot",
        snapshot_then_replace_public_projection,
    )

    try:
        indexer.bind_repository_source(binding)

        assert indexer.project_root == str(repo)
        assert indexer.source_mode == bm25_module.SOURCE_MODE_BOUND_REPOSITORY
    finally:
        binding.close()


def test_absolute_source_beneath_root_is_accepted(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    source = repo / "pkg" / "source.py"
    source.parent.mkdir(parents=True)
    source.write_text("contained absolute source\n", encoding="utf-8")

    indexer = _indexer(repo, str(source), node_type=NODE_TYPE_FILE)

    assert _content(indexer) == "contained absolute source\n"


@pytest.mark.parametrize("outside_path", ["absolute", "parent"])
def test_source_outside_root_is_rejected(tmp_path: Path, outside_path: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("outside secret\n", encoding="utf-8")
    file_path = str(outside) if outside_path == "absolute" else "../outside.py"

    indexer = _indexer(repo, file_path, node_type=NODE_TYPE_FILE)

    assert _content(indexer) is None


def test_identifier_occurrence_scan_uses_the_same_source_boundary(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("outside_call()\n", encoding="utf-8")
    indexer = _indexer(repo, "../outside.py")

    assert indexer.search_identifier_occurrences("outside_call") == []


@pytest.mark.parametrize("embedded_break", _NON_LF_LINE_BREAKS)
def test_identifier_range_does_not_treat_non_lf_characters_as_lines(
    tmp_path: Path,
    embedded_break: str,
) -> None:
    repo = tmp_path / "repo"
    source = repo / "source.py"
    repo.mkdir()
    first_line = f"prefix{embedded_break}outside_call()\n"
    source.write_text(first_line + "second\n", encoding="utf-8")
    indexer = _indexer(repo, "source.py", start_line=0, end_line=0)

    results = indexer.search_identifier_occurrences(
        "outside_call",
        wrap_with_ln=False,
    )

    assert [result.content for result in results] == [first_line]


@pytest.mark.skipif(os.name != "nt", reason="drive-relative paths are Windows-only")
def test_source_on_a_different_windows_drive_is_rejected(tmp_path: Path) -> None:
    root_drive, _tail = os.path.splitdrive(str(tmp_path))
    other_drive = "Z:" if root_drive.upper() != "Z:" else "Y:"

    assert (
        bm25_module._contained_source_path(
            tmp_path,
            other_drive + r"\outside.py",
        )
        is None
    )


def test_absolute_contained_final_source_symlink_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "real.py"
    target.write_text("contained alias target\n", encoding="utf-8")
    _symlink_or_skip(repo / "source.py", target)

    indexer = _indexer(repo, "source.py", node_type=NODE_TYPE_FILE)

    assert _content(indexer) is None


def test_relative_final_source_symlink_inside_root_is_accepted(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "real.py").write_text("contained alias target\n", encoding="utf-8")
    _symlink_or_skip(repo / "source.py", Path("real.py"))

    indexer = _indexer(repo, "source.py", node_type=NODE_TYPE_FILE)

    assert _content(indexer) == "contained alias target\n"


def test_relative_intermediate_symlink_with_parent_inside_root_is_accepted(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    real = repo / "real" / "source.py"
    real.parent.mkdir(parents=True)
    real.write_text("inside_call()\n", encoding="utf-8")
    links = repo / "links"
    links.mkdir()
    _symlink_or_skip(links / "pkg", Path("../real"), directory=True)
    indexer = _indexer(repo, "links/pkg/source.py", node_type=NODE_TYPE_FILE)

    assert _content(indexer) == "inside_call()\n"
    occurrences = indexer.search_identifier_occurrences(
        "inside_call",
        wrap_with_ln=False,
    )
    assert [item.content for item in occurrences] == ["inside_call()\n"]


@pytest.mark.parametrize("target", ["../outside.py", "missing.py"])
def test_relative_final_source_symlink_outside_or_broken_is_rejected(
    tmp_path: Path,
    target: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "outside.py").write_text("outside secret\n", encoding="utf-8")
    _symlink_or_skip(repo / "source.py", Path(target))

    indexer = _indexer(repo, "source.py", node_type=NODE_TYPE_FILE)

    assert _content(indexer) is None


def test_source_symlink_loop_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _symlink_or_skip(repo / "source.py", Path("other.py"))
    _symlink_or_skip(repo / "other.py", Path("source.py"))

    assert _content(_indexer(repo, "source.py", node_type=NODE_TYPE_FILE)) is None


def test_source_symlink_hop_limit_is_enforced(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "real.py").write_text("safe\n", encoding="utf-8")
    for index in range(41):
        target = f"link-{index + 1}.py" if index < 40 else "real.py"
        _symlink_or_skip(repo / f"link-{index}.py", Path(target))

    assert _content(_indexer(repo, "link-0.py", node_type=NODE_TYPE_FILE)) is None


def test_source_symlink_reversible_swap_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "real.py").write_text("safe\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("outside secret\n", encoding="utf-8")
    link = repo / "source.py"
    _symlink_or_skip(link, Path("real.py"))
    real_verify = contained_source_module._BoundRepositoryFile.verify
    calls = 0

    def swap_then_restore(binding) -> None:
        nonlocal calls
        calls += 1
        if calls != 2:
            return real_verify(binding)
        link.unlink()
        link.symlink_to(Path("../outside.py"))
        try:
            return real_verify(binding)
        finally:
            link.unlink()
            link.symlink_to(Path("real.py"))

    monkeypatch.setattr(
        contained_source_module._BoundRepositoryFile,
        "verify",
        swap_then_restore,
    )

    assert _content(_indexer(repo, "source.py", node_type=NODE_TYPE_FILE)) is None
    assert calls == 2


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="requires procfs")
def test_contained_symlink_reads_do_not_leak_descriptors(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "real.py").write_text("safe\n", encoding="utf-8")
    _symlink_or_skip(repo / "source.py", Path("real.py"))
    indexer = _indexer(repo, "source.py", node_type=NODE_TYPE_FILE)
    before = len(list(Path("/proc/self/fd").iterdir()))

    for _ in range(20):
        assert _content(indexer) == "safe\n"

    assert len(list(Path("/proc/self/fd").iterdir())) == before


def test_intermediate_source_symlink_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    (outside / "source.py").write_text("outside secret\n", encoding="utf-8")
    _symlink_or_skip(repo / "pkg", outside, directory=True)

    indexer = _indexer(repo, "pkg/source.py", node_type=NODE_TYPE_FILE)

    assert _content(indexer) is None


def test_static_fallback_reads_an_ordinary_contained_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    source = repo / "pkg" / "source.py"
    source.parent.mkdir(parents=True)
    source.write_text("portable source\n", encoding="utf-8")
    monkeypatch.setattr(bm25_module, "_SECURE_SOURCE_DIRECTORY_FDS", False)

    indexer = _indexer(repo, "pkg/source.py", node_type=NODE_TYPE_FILE)

    assert _content(indexer) == "portable source\n"


@pytest.mark.parametrize("zero_component", ["root", "intermediate", "final"])
def test_static_fallback_does_not_open_a_zero_identity_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    zero_component: str,
) -> None:
    repo = tmp_path / "repo"
    package = repo / "pkg"
    source = package / "source.py"
    package.mkdir(parents=True)
    source.write_text("safe source\n", encoding="utf-8")
    targets = {
        "root": repo,
        "intermediate": package,
        "final": source,
    }
    zero_path = os.path.normcase(os.path.abspath(targets[zero_component]))
    real_lstat = os.lstat

    def zero_identity_lstat(path: str | os.PathLike[str]) -> os.stat_result:
        metadata = real_lstat(path)
        if os.path.normcase(os.path.abspath(path)) == zero_path:
            return _ZeroIdentityMetadata(metadata)  # type: ignore[return-value]
        return metadata

    def forbidden_open(*_args, **_kwargs):
        raise AssertionError("zero-identity source paths must fail before open")

    monkeypatch.setattr(bm25_module, "_SECURE_SOURCE_DIRECTORY_FDS", False)
    monkeypatch.setattr(bm25_module.os, "lstat", zero_identity_lstat)
    monkeypatch.setattr(bm25_module.os, "open", forbidden_open)
    indexer = _indexer(repo, "pkg/source.py", node_type=NODE_TYPE_FILE)

    assert _content(indexer) is None


def test_static_fallback_zero_identity_swap_and_restore_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    package = repo / "pkg"
    source = package / "source.py"
    package.mkdir(parents=True)
    source.write_text("SAFE\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "source.py").write_text("OUTSIDE\n", encoding="utf-8")
    moved_package = repo / "saved-pkg"
    real_lstat = os.lstat
    real_fstat = os.fstat
    real_open = os.open
    real_read = bm25_module._read_open_descriptor
    open_attempted = False
    restore_attempted = False

    def zero_identity_lstat(path: str | os.PathLike[str]) -> os.stat_result:
        return _ZeroIdentityMetadata(real_lstat(path))  # type: ignore[return-value]

    def zero_identity_fstat(descriptor: int) -> os.stat_result:
        return _ZeroIdentityMetadata(real_fstat(descriptor))  # type: ignore[return-value]

    def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal open_attempted
        open_attempted = True
        package.rename(moved_package)
        _symlink_or_skip(package, outside, directory=True)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def restoring_read(descriptor: int) -> str | None:
        nonlocal restore_attempted
        try:
            return real_read(descriptor)
        finally:
            package.unlink()
            moved_package.rename(package)
            restore_attempted = True

    monkeypatch.setattr(bm25_module, "_SECURE_SOURCE_DIRECTORY_FDS", False)
    monkeypatch.setattr(bm25_module.os, "lstat", zero_identity_lstat)
    monkeypatch.setattr(bm25_module.os, "fstat", zero_identity_fstat)
    monkeypatch.setattr(bm25_module.os, "open", swapping_open)
    monkeypatch.setattr(bm25_module, "_read_open_descriptor", restoring_read)
    indexer = _indexer(repo, "pkg/source.py", node_type=NODE_TYPE_FILE)

    assert _content(indexer) is None
    assert not open_attempted
    assert not restore_attempted


def test_static_fallback_zero_fstat_identity_is_not_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    source = repo / "source.py"
    repo.mkdir()
    source.write_text("safe source\n", encoding="utf-8")
    real_fstat = os.fstat
    read_attempted = False

    def zero_identity_fstat(descriptor: int) -> os.stat_result:
        return _ZeroIdentityMetadata(real_fstat(descriptor))  # type: ignore[return-value]

    def forbidden_read(_descriptor: int) -> str | None:
        nonlocal read_attempted
        read_attempted = True
        raise AssertionError("zero-identity source descriptors must not be read")

    monkeypatch.setattr(bm25_module, "_SECURE_SOURCE_DIRECTORY_FDS", False)
    monkeypatch.setattr(bm25_module.os, "fstat", zero_identity_fstat)
    monkeypatch.setattr(bm25_module, "_read_open_descriptor", forbidden_read)
    indexer = _indexer(repo, "source.py", node_type=NODE_TYPE_FILE)

    assert _content(indexer) is None
    assert not read_attempted


def test_static_fallback_rejects_a_final_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("outside secret\n", encoding="utf-8")
    _symlink_or_skip(repo / "source.py", outside)
    monkeypatch.setattr(bm25_module, "_SECURE_SOURCE_DIRECTORY_FDS", False)

    indexer = _indexer(repo, "source.py", node_type=NODE_TYPE_FILE)

    assert _content(indexer) is None


def test_static_fallback_rejects_a_reparse_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    source = repo / "source.py"
    repo.mkdir()
    source.write_text("reparse source\n", encoding="utf-8")
    real_lstat = os.lstat

    class _ReparseMetadata:
        st_file_attributes = bm25_module._FILE_ATTRIBUTE_REPARSE_POINT

        def __init__(self, wrapped: os.stat_result) -> None:
            self._wrapped = wrapped

        def __getattr__(self, name: str):
            return getattr(self._wrapped, name)

    def reparse_lstat(path: str | os.PathLike[str]) -> os.stat_result:
        metadata = real_lstat(path)
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(str(source)):
            return _ReparseMetadata(metadata)  # type: ignore[return-value]
        return metadata

    monkeypatch.setattr(bm25_module, "_SECURE_SOURCE_DIRECTORY_FDS", False)
    monkeypatch.setattr(bm25_module.os, "lstat", reparse_lstat)
    indexer = _indexer(repo, "source.py", node_type=NODE_TYPE_FILE)

    assert _content(indexer) is None


def test_static_fallback_rejects_an_intermediate_link_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    package = repo / "pkg"
    package.mkdir(parents=True)
    source = package / "source.py"
    source.write_text("safe source\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "source.py").write_text("outside secret\n", encoding="utf-8")
    real_open = os.open
    swapped = False

    def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and dir_fd is None and os.fspath(path) == str(source):
            package.rename(repo / "old-pkg")
            _symlink_or_skip(package, outside, directory=True)
            swapped = True
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(bm25_module, "_SECURE_SOURCE_DIRECTORY_FDS", False)
    monkeypatch.setattr(bm25_module.os, "open", swapping_open)
    indexer = _indexer(repo, "pkg/source.py", node_type=NODE_TYPE_FILE)

    assert _content(indexer) is None
    assert swapped


@pytest.mark.skipif(
    not bm25_module._SECURE_SOURCE_DIRECTORY_FDS,
    reason="descriptor-relative swap coverage requires POSIX safe-open flags",
)
def test_final_component_swap_to_symlink_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    source = repo / "source.py"
    repo.mkdir()
    source.write_text("safe source\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("outside secret\n", encoding="utf-8")
    real_open = os.open
    swapped = False

    def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and dir_fd is not None and os.fspath(path) == "source.py":
            source.unlink()
            _symlink_or_skip(source, outside)
            swapped = True
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(bm25_module.os, "open", swapping_open)
    indexer = _indexer(repo, "source.py", node_type=NODE_TYPE_FILE)

    assert _content(indexer) is None
    assert swapped


@pytest.mark.skipif(
    not bm25_module._SECURE_SOURCE_DIRECTORY_FDS,
    reason="descriptor-relative swap coverage requires POSIX safe-open flags",
)
def test_intermediate_component_swap_to_symlink_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    package = repo / "pkg"
    package.mkdir(parents=True)
    (package / "source.py").write_text("safe source\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "source.py").write_text("outside secret\n", encoding="utf-8")
    real_open = os.open
    swapped = False

    def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and dir_fd is not None and os.fspath(path) == "pkg":
            package.rename(repo / "old-pkg")
            _symlink_or_skip(package, outside, directory=True)
            swapped = True
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(bm25_module.os, "open", swapping_open)
    indexer = _indexer(repo, "pkg/source.py", node_type=NODE_TYPE_FILE)

    assert _content(indexer) is None
    assert swapped


def test_index_without_project_root_keeps_absolute_path_compatibility(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy.py"
    source.write_text("legacy absolute source\n", encoding="utf-8")
    indexer = _indexer(None, str(source), node_type=NODE_TYPE_FILE)

    assert _content(indexer) == "legacy absolute source\n"


@pytest.mark.skipif(
    not bm25_module._SECURE_SOURCE_DIRECTORY_FDS,
    reason="descriptor leak coverage requires POSIX safe-open flags",
)
def test_successful_source_read_closes_every_opened_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    source = repo / "pkg" / "source.py"
    source.parent.mkdir(parents=True)
    source.write_text("safe source\n", encoding="utf-8")
    real_open = os.open
    opened: list[int] = []

    def tracking_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            descriptor = real_open(path, flags, mode)
        else:
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(bm25_module.os, "open", tracking_open)
    indexer = _indexer(repo, "pkg/source.py", node_type=NODE_TYPE_FILE)

    assert _content(indexer) == "safe source\n"
    assert opened
    for descriptor in set(opened):
        with pytest.raises(OSError) as exc_info:
            os.fstat(descriptor)
        assert exc_info.value.errno == errno.EBADF


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
@pytest.mark.timeout(2)
def test_non_regular_source_is_rejected_without_blocking(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    os.mkfifo(repo / "source.py")
    indexer = _indexer(repo, "source.py", node_type=NODE_TYPE_FILE)

    assert _content(indexer) is None
