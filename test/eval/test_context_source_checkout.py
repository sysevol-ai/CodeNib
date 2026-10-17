"""Real local Git fixtures: borrowed objects never share mutable source trees."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import contextbench_source_checkout as checkout


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], stderr=subprocess.PIPE, text=True
    ).strip()


@pytest.fixture
def repositories(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q")
    git(origin, "config", "user.name", "Source test")
    git(origin, "config", "user.email", "source@example.invalid")
    git(origin, "config", "gc.auto", "0")
    git(origin, "config", "maintenance.auto", "false")
    (origin / "a.py").write_text("old\n")
    (origin / "common.py").write_text("unchanged\n")
    git(origin, "add", ".")
    git(origin, "commit", "-qm", "Initial source")
    old = git(origin, "rev-parse", "HEAD")
    (origin / "a.py").write_text("new\n")
    git(origin, "commit", "-qam", "Change source")
    new = git(origin, "rev-parse", "HEAD")
    reference = tmp_path / "reference"
    reference.mkdir()
    git(reference, "init", "-q")
    git(reference, "config", "gc.auto", "0")
    git(reference, "config", "maintenance.auto", "false")
    git(reference, "remote", "add", "origin", origin.as_uri())
    git(reference, "fetch", "--depth", "1", "origin", old)
    git(reference, "checkout", "--detach", old)
    case = SimpleNamespace(
        instance_id="source-case", repo_url=origin.as_uri(), base_commit=new
    )
    return SimpleNamespace(
        origin=origin,
        reference=reference,
        old=old,
        new=new,
        case=case,
        root=tmp_path / "staged",
    )


def prepare(r, **kwargs):
    return checkout.prepare_checkout(
        r.case,
        r.root,
        reference=r.reference,
        reference_commit=r.old,
        minimum_free_bytes=0,
        **kwargs,
    )


def test_shallow_reference_publishes_exact_tree_without_mutating_donor(repositories):
    r = repositories
    original_config = (r.reference / ".git/config").read_bytes()
    result = prepare(r)
    assert checkout.audit_contextbench_snapshot(result, r.new)["clean"]
    assert (result / "a.py").read_text() == "new\n"
    assert git(r.reference, "rev-parse", "HEAD") == r.old
    assert (r.reference / "a.py").read_text() == "old\n"
    assert (r.reference / ".git/config").read_bytes() == original_config
    alternates = (result / ".git/objects/info/alternates").read_text().strip()
    assert Path(alternates).resolve() == r.reference / ".git/objects"
    assert git(result, "config", "remote.origin.url") == r.origin.as_uri()
    assert git(result, "config", "gc.auto") == "0"
    (result / "common.py").write_text("borrower edit\n")
    assert (r.reference / "common.py").read_text() == "unchanged\n"


def test_existing_valid_checkout_needs_no_new_fetch(repositories, monkeypatch):
    r = repositories
    first = prepare(r)

    def unexpected(*args):
        pytest.fail("A valid existing checkout must not run preparation Git commands")

    monkeypatch.setattr(checkout, "_git", unexpected)
    assert prepare(r) == first


def test_existing_dirty_checkout_is_never_reset_or_cleaned(repositories):
    r = repositories
    destination = prepare(r)
    (destination / "a.py").write_text("owned local edit\n")
    with pytest.raises(ValueError, match="snapshot audit failed"):
        prepare(r)
    assert (destination / "a.py").read_text() == "owned local edit\n"


def test_disk_floor_prevents_new_staging(repositories, monkeypatch):
    r = repositories
    monkeypatch.setattr(
        checkout.shutil, "disk_usage", lambda *a: SimpleNamespace(free=0)
    )
    with pytest.raises(OSError, match="disk floor"):
        checkout.prepare_checkout(
            r.case,
            r.root,
            reference=r.reference,
            reference_commit=r.old,
            minimum_free_bytes=1,
        )
    assert list(r.root.iterdir()) == []


def test_failed_fetch_removes_only_owned_staging(repositories):
    r = repositories
    r.case.base_commit = "f" * 40
    with pytest.raises(subprocess.CalledProcessError):
        prepare(r)
    assert list(r.root.iterdir()) == []
    assert checkout.audit_contextbench_snapshot(r.reference, r.old)["clean"]


def test_donor_changes_during_fetch_cannot_publish_borrower(repositories, monkeypatch):
    r = repositories
    original = checkout._git

    def mutate(repo, *args):
        result = original(repo, *args)
        if args[0] == "fetch":
            (r.reference / "a.py").write_text("concurrent owner edit\n")
        return result

    monkeypatch.setattr(checkout, "_git", mutate)
    with pytest.raises(ValueError, match="snapshot audit failed"):
        prepare(r)
    assert list(r.root.iterdir()) == []
    assert (r.reference / "a.py").read_text() == "concurrent owner edit\n"


def test_reference_identity_and_task_path_are_explicit(repositories):
    r = repositories
    with pytest.raises(ValueError, match="captured commit"):
        checkout.prepare_checkout(r.case, r.root, reference=r.reference)
    r.case.instance_id = "../escape"
    with pytest.raises(ValueError, match="task identity"):
        prepare(r)
