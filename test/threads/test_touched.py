# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import subprocess

import pytest

from codenib.threads.touched import merge_base, parse_unified_diff, touched_between

BASE = """\
pub struct Foo {
    a: u32,
}

impl Foo {
    pub fn bar(&self) -> u32 {
        self.a
    }

    fn baz(&self) -> u32 {
        1
    }
}

pub fn top(x: u32) -> u32 {
    x + 1
}
"""


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
    )


def _branch(repo, name, old, new):
    _git(repo, "checkout", "-q", "-b", name, "main")
    path = repo / "src" / "lib.rs"
    path.write_text(path.read_text().replace(old, new))
    _git(repo, "commit", "-qam", name)


@pytest.fixture()
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(BASE)
    (tmp_path / "notes.txt").write_text("one\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    _branch(tmp_path, "edit-bar", "self.a\n", "self.a + 1\n")
    _branch(tmp_path, "edit-bar-too", "self.a\n", "self.a * 2\n")
    _branch(tmp_path, "edit-top", "x + 1\n", "x + 2\n")
    return tmp_path


def _touched(repo, branch):
    return touched_between(repo, merge_base(repo, "main", branch), branch)


def test_parse_unified_diff_ranges():
    diff = (
        "diff --git a/x.rs b/x.rs\n--- a/x.rs\n+++ b/x.rs\n"
        "@@ -3 +3,2 @@ fn a()\n-old\n+new\n+new2\n"
        "@@ -9,0 +11 @@\n+added\n"
    )
    (change,) = parse_unified_diff(diff)
    assert change.old_path == change.new_path == "x.rs"
    assert change.old_ranges == ((3, 3),)
    assert change.new_ranges == ((3, 4), (11, 11))


def test_changed_line_resolves_to_innermost_symbol(repo):
    touched = _touched(repo, "edit-bar")
    assert touched.files == {"src/lib.rs"}
    assert touched.symbols == {"src/lib.rs:Foo.bar()"}
    assert touched.mapped_lines == touched.source_lines == 2


def test_missing_commit_returns_none(repo):
    assert touched_between(repo, "main", "0" * 40) is None


def test_impl_header_is_not_a_shared_symbol(repo):
    _branch(repo, "edit-impl", "impl Foo {", "impl Foo {\n    const N: u32 = 1;\n")
    touched = _touched(repo, "edit-impl")
    assert not any(symbol.endswith(":impl") for symbol in touched.symbols)


def test_parse_unified_diff_unquotes_c_style_paths():
    diff = (
        'diff --git "a/say \\"hi\\".rs" "b/say \\"hi\\".rs"\n'
        '--- "a/say \\"hi\\".rs"\n'
        '+++ "b/say \\"hi\\".rs"\n'
        "@@ -2 +2 @@\n"
        "-old\n"
        "+new\n"
        'diff --git "a/tab\\there\\\\x.rs" "b/tab\\there\\\\x.rs"\n'
        '--- "a/tab\\there\\\\x.rs"\n'
        '+++ "b/tab\\there\\\\x.rs"\n'
        "@@ -5 +5 @@\n"
        "-old\n"
        "+new\n"
        'diff --git "a/new\\303\\251.rs" "b/new\\303\\251.rs"\n'
        "--- /dev/null\n"
        '+++ "b/new\\303\\251.rs"\n'
        "@@ -0,0 +1 @@\n"
        "+added\n"
    )
    changes = parse_unified_diff(diff)
    assert len(changes) == 3
    assert changes[0].old_path == changes[0].new_path == 'say "hi".rs'
    assert changes[1].old_path == changes[1].new_path == "tab\there\\x.rs"
    assert changes[2].old_path is None
    assert changes[2].new_path == "newé.rs"


def test_touched_between_handles_quoted_git_paths(repo):
    path = repo / "src" / "café.rs"
    path.write_text("pub fn helper() -> u32 {\n    1\n}\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "add-cafe")
    _git(repo, "checkout", "-q", "-b", "edit-cafe")
    path.write_text("pub fn helper() -> u32 {\n    2\n}\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "edit-cafe")
    touched = touched_between(repo, "HEAD~1", "HEAD")
    assert touched is not None
    assert "src/café.rs" in touched.files
    assert "src/café.rs:helper()" in touched.symbols
