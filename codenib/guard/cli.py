# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""``codenib guard``: tests at risk and notes for the current change."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _repo(value: str) -> Path:
    from ..cli import resolve_repo_path
    from .snapshot import SnapshotError, git

    path = resolve_repo_path(value)
    try:
        return Path(git(path, "rev-parse", "--show-toplevel").strip())
    except SnapshotError as exc:
        raise _error(f"{path} is not inside a git repository") from exc


def _error(message: str):
    from ..cli import CLIError

    return CLIError(message)


def _run_check(args: argparse.Namespace) -> int:
    from .notes import NoteError
    from .report import render_text, run_guard
    from .snapshot import SnapshotError

    repo = _repo(args.repo)
    try:
        report = run_guard(repo, args.base, depth=args.depth, build=not args.no_build)
    except (SnapshotError, NoteError) as exc:
        raise _error(str(exc)) from exc
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render_text(report))
    return 1 if args.exit_code and report.impact.tests else 0


def _run_prepare(args: argparse.Namespace) -> int:
    from .snapshot import SnapshotError, build_snapshot, resolve_commit

    repo = _repo(args.repo)
    try:
        commit = resolve_commit(repo, args.commit)
        snapshot = build_snapshot(repo, commit)
    except SnapshotError as exc:
        raise _error(str(exc)) from exc
    took = "" if snapshot.seconds is None else f" in {snapshot.seconds:.0f} s"
    print(f"Graph of {commit[:12]} ready{took}.")
    return 0


def _agents(selected) -> list:
    from .hooks import AGENTS

    chosen = selected or ["claude"]
    return list(AGENTS) if "all" in chosen else list(dict.fromkeys(chosen))


def _run_install(args: argparse.Namespace) -> int:
    from .hooks import install, set_first_run_sharing
    from .snapshot import SnapshotError, build_snapshot, resolve_commit

    repo = _repo(args.repo)
    for agent in _agents(args.agent):
        path = install(repo, agent, scope=args.scope)
        print(f"{agent}: added SessionStart and Stop hooks to {path}")
        if agent == "codex":
            print(
                "  Codex runs a new hook only after you trust it: open /hooks in Codex."
            )
    if args.share_first_run:
        set_first_run_sharing(True)
        print(
            "First-run report on: the first time the hook reports, it sends one "
            "anonymous event (agent name and CodeNib version) to codenib.ai."
        )
    if args.prepare:
        try:
            commit = resolve_commit(repo)
            print(f"Building the graph of {commit[:12]}; this takes about a minute...")
            build_snapshot(repo, commit)
        except SnapshotError as exc:
            print(
                f"warning: {exc}. The hook will retry when a session starts.",
                file=sys.stderr,
            )
        else:
            print("Graph ready.")
    print(
        "Done. Before an agent finishes, the guard lists the tests its change reaches."
    )
    return 0


def _run_uninstall(args: argparse.Namespace) -> int:
    from .hooks import uninstall

    repo = _repo(args.repo)
    for agent in _agents(args.agent):
        path = uninstall(repo, agent, scope=args.scope)
        done = f"removed the guard hooks from {path}" if path else "nothing to remove"
        print(f"{agent}: {done}")
    return 0


def _run_hook(args: argparse.Namespace) -> int:
    from .hooks import run_hook

    return run_hook(args.agent, args.event, sys.stdin.read())


def _run_note_add(args: argparse.Namespace) -> int:
    from .notes import NoteError, add_note

    repo = _repo(args.repo)
    try:
        note = add_note(
            repo, args.text, args.anchor, kind=args.kind, source=args.source or ""
        )
    except NoteError as exc:
        raise _error(str(exc)) from exc
    print(f"Wrote {note.path}")
    return 0


def _run_note_list(args: argparse.Namespace) -> int:
    from .notes import anchor_states, load_notes

    repo = _repo(args.repo)
    notes, errors = load_notes(repo)
    for note in notes:
        states = anchor_states(repo, note)
        flag = (
            ""
            if all(s.state == "current" for s in states)
            else "  [may be out of date]"
        )
        print(f"{note.path}  [{note.kind}] {note.title}{flag}")
        for state in states:
            print(f"    {state.anchor.symbol}  {state.state}")
    for error in errors:
        print(f"unreadable: {error}", file=sys.stderr)
    if not notes:
        print("No notes yet. Add one with `codenib guard note add`.")
    return 1 if errors else 0


def _run_note_confirm(args: argparse.Namespace) -> int:
    from .notes import NoteError, confirm_note, load_note

    repo = _repo(args.repo)
    try:
        path = Path(args.note)
        path = path.resolve().relative_to(repo) if path.is_absolute() else path
        note = confirm_note(repo, load_note(repo, path))
    except (OSError, NoteError, ValueError) as exc:
        raise _error(str(exc)) from exc
    print(f"Confirmed {note.path} against the current code.")
    return 0


def add_guard_parser(subparsers, formatter) -> None:
    guard = subparsers.add_parser(
        "guard",
        help="list the tests and notes that the current change reaches",
        formatter_class=formatter,
    )
    commands = guard.add_subparsers(dest="guard_command", required=True)

    check = commands.add_parser(
        "check",
        help="report tests at risk and notes for changes since a revision",
        formatter_class=formatter,
    )
    check.add_argument("repo", nargs="?", default=".")
    check.add_argument(
        "--base", default="HEAD", help="compare the working tree with this revision"
    )
    check.add_argument(
        "--depth", type=int, default=3, help="calls between a test and the change"
    )
    check.add_argument(
        "--json", action="store_true", help="print a machine-readable report"
    )
    check.add_argument(
        "--no-build",
        action="store_true",
        help="fail instead of building a missing graph",
    )
    check.add_argument(
        "--exit-code", action="store_true", help="exit 1 when any test is at risk"
    )
    check.set_defaults(handler=_run_check)

    prepare = commands.add_parser(
        "prepare",
        help="build the graph of a commit ahead of time",
        formatter_class=formatter,
    )
    prepare.add_argument("repo", nargs="?", default=".")
    prepare.add_argument("--commit", default="HEAD")
    prepare.set_defaults(handler=_run_prepare)

    install = commands.add_parser(
        "install",
        help="run the guard before Claude Code or Codex finishes",
        formatter_class=formatter,
    )
    install.add_argument("repo", nargs="?", default=".")
    install.add_argument(
        "--agent",
        action="append",
        choices=("claude", "codex", "all"),
        help="default: claude",
    )
    install.add_argument(
        "--scope",
        choices=("local", "project", "user"),
        default="local",
        help=(
            "local: .claude/settings.local.json (Codex: .codex/hooks.json); "
            "project: the shared project settings; user: your home settings"
        ),
    )
    install.add_argument(
        "--share-first-run",
        action="store_true",
        help="send one anonymous event the first time the hook reports",
    )
    install.add_argument(
        "--prepare",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="build the graph of HEAD now",
    )
    install.set_defaults(handler=_run_install)

    uninstall = commands.add_parser(
        "uninstall", help="remove the guard hooks", formatter_class=formatter
    )
    uninstall.add_argument("repo", nargs="?", default=".")
    uninstall.add_argument(
        "--agent", action="append", choices=("claude", "codex", "all")
    )
    uninstall.add_argument(
        "--scope", choices=("local", "project", "user"), default="local"
    )
    uninstall.set_defaults(handler=_run_uninstall)

    hook = commands.add_parser(
        "hook", help="entry point for agent hooks (reads the event on stdin)"
    )
    hook.add_argument("--agent", choices=("claude", "codex"), required=True)
    hook.add_argument("--event", choices=("session-start", "stop"), required=True)
    hook.set_defaults(handler=_run_hook)

    note = commands.add_parser(
        "note", help="notes anchored to code", formatter_class=formatter
    )
    note_commands = note.add_subparsers(dest="note_command", required=True)
    add = note_commands.add_parser(
        "add",
        help="record a note about one or more definitions",
        formatter_class=formatter,
    )
    add.add_argument("text", help="the note; its first line is its title")
    add.add_argument(
        "--anchor",
        action="append",
        required=True,
        help="definition the note is about, as path/to/file.py:Class.method",
    )
    add.add_argument(
        "--kind",
        default="note",
        choices=("decision", "gotcha", "invariant", "contract", "note"),
    )
    add.add_argument("--source", help="link to where this was learned, such as a PR")
    add.add_argument("--repo", default=".")
    add.set_defaults(handler=_run_note_add)
    listing = note_commands.add_parser(
        "list", help="list notes and whether their code changed"
    )
    listing.add_argument("repo", nargs="?", default=".")
    listing.set_defaults(handler=_run_note_list)
    confirm = note_commands.add_parser(
        "confirm", help="mark a note as still true for the current code"
    )
    confirm.add_argument("note", help="the note file")
    confirm.add_argument("--repo", default=".")
    confirm.set_defaults(handler=_run_note_confirm)
