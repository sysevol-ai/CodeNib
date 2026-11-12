# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""``codenib threads``: issue and pull request context inside a token budget."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

_REMOTE_RE = re.compile(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$")


def _error(message: str):
    from ..cli import CLIError

    return CLIError(message)


def _checkout(value: str | None) -> Path | None:
    path = Path(value or ".").expanduser().resolve()
    proc = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        if value:
            raise _error(f"{path} is not a git repository")
        return None
    return path


def _repo_name(args: argparse.Namespace, checkout: Path | None) -> str:
    if args.repo:
        return args.repo
    if checkout is not None:
        proc = subprocess.run(
            ["git", "-C", str(checkout), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
        )
        match = _REMOTE_RE.search(proc.stdout.strip())
        if match:
            return match.group(1)
    raise _error("pass --repo owner/name, or run inside a clone of a GitHub repository")


def _store(args: argparse.Namespace, checkout: Path | None):
    from .log_store import ThreadStore

    root = Path(args.store).expanduser() if args.store else None
    try:
        return ThreadStore(_repo_name(args, checkout), root)
    except ValueError as exc:
        raise _error(str(exc)) from exc


def _sync(store, numbers: list[int], *, refresh_linked: bool, quiet: bool) -> None:
    from .github_source import GitHubClient, resolve_token
    from .sync import sync_thread

    client = GitHubClient(resolve_token())
    for number in numbers:
        try:
            written = sync_thread(
                client,
                store,
                number,
                refresh_linked=refresh_linked,
                on_progress=(
                    None if quiet else (lambda msg: print(f"  {msg}", file=sys.stderr))
                ),
            )
        except RuntimeError as exc:
            raise _error(str(exc)) from exc
        if not written:
            raise _error(f"{store.repo}#{number} was not found")
        if not quiet:
            total = sum(written.values())
            print(
                f"#{number}: {total} new events across {len(written)} threads",
                file=sys.stderr,
            )


def _sync_open_pulls(store, limit: int, quiet: bool) -> None:
    from .github_source import GitHubClient, resolve_token
    from .in_flight import fetch_open_pulls, write_index

    try:
        pulls = fetch_open_pulls(GitHubClient(resolve_token()), store.repo, limit)
    except RuntimeError as exc:
        raise _error(str(exc)) from exc
    write_index(store, pulls)
    if not quiet:
        print(f"indexed {len(pulls)} open pull requests", file=sys.stderr)


def _run_sync(args: argparse.Namespace) -> int:
    if not args.numbers and not args.open_pulls:
        raise _error("name a thread number, or pass --open-pulls")
    store = _store(args, _checkout(args.checkout))
    _sync(store, args.numbers, refresh_linked=args.refresh_linked, quiet=args.quiet)
    if args.open_pulls:
        _sync_open_pulls(store, args.open_pulls, args.quiet)
    return 0


def _packet(args: argparse.Namespace):
    from .in_flight import read_index
    from .serve import build_packet, load_bundle

    checkout = _checkout(args.checkout)
    store = _store(args, checkout)
    if not args.offline and (args.sync or not store.has(args.number)):
        _sync(store, [args.number], refresh_linked=args.sync, quiet=True)
    bundle = load_bundle(store, args.number, at=args.at)
    if bundle is None:
        raise _error(
            f"{store.repo}#{args.number} is not in the store; "
            f"run `codenib threads sync {args.number}`"
        )
    try:
        return build_packet(
            bundle,
            budget=args.budget,
            code_budget=args.code_budget,
            since=args.since,
            checkout=None if args.no_anchors else checkout,
            rev=args.rev,
            open_pulls=read_index(store),
        )
    except KeyError as exc:
        raise _error(str(exc.args[0])) from exc


def _run_show(args: argparse.Namespace) -> int:
    packet = _packet(args)
    if args.json:
        print(json.dumps(packet.to_json(), indent=2))
    else:
        sys.stdout.write(packet.text)
    return 0


def _run_context(args: argparse.Namespace) -> int:
    packet = _packet(args)
    target = Path(args.output).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(packet.text, encoding="utf-8")
    print(f"Wrote {target} ({packet.tokens} tokens, cursor {packet.head})")
    return 0


def _run_events(args: argparse.Namespace) -> int:
    from .serve import short

    checkout = _checkout(args.checkout)
    store = _store(args, checkout)
    wanted = {short(a) for a in args.addresses}
    numbers = {int(m.group(1)) for a in wanted if (m := re.match(r"#(\d+)/", a))}
    found = []
    for number in sorted(numbers):
        found += [e for e in store.events(number) if short(e.address) in wanted]
    if not found:
        raise _error("no stored event has that address")
    if args.json:
        print(json.dumps([e.to_json() for e in found], indent=2))
        return 0
    for event in found:
        print(f"[{short(event.address)}] {event.at} {event.kind} by {event.actor}")
        for part in (event.meta.get("hunk"), event.body, event.meta.get("log")):
            if part:
                print(part)
        print()
    return 0


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", help="owner/name; default: the origin remote")
    parser.add_argument(
        "--checkout", help="local clone used to resolve code locations (default: .)"
    )
    parser.add_argument(
        "--store", help="thread store directory (default: CodeNib home)"
    )


def _serving(parser: argparse.ArgumentParser) -> None:
    from .serve import DEFAULT_BUDGET, DEFAULT_CODE_BUDGET

    parser.add_argument("number", type=int, help="issue or pull request number")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help="max tokens")
    parser.add_argument(
        "--code-budget",
        type=int,
        default=DEFAULT_CODE_BUDGET,
        help="extra tokens for the source of the symbols the thread points at",
    )
    parser.add_argument(
        "--since", help="event id from an earlier packet; return only what followed it"
    )
    parser.add_argument("--rev", default="HEAD", help="commit to resolve code against")
    parser.add_argument("--at", help="ISO time; serve the thread as it was then")
    parser.add_argument("--sync", action="store_true", help="refetch before serving")
    parser.add_argument("--offline", action="store_true", help="never call GitHub")
    parser.add_argument(
        "--no-anchors", action="store_true", help="skip code location resolution"
    )
    _common(parser)


def add_threads_parser(subparsers, formatter) -> None:
    parser = subparsers.add_parser(
        "threads",
        help="serve issue and pull request context inside a token budget",
        formatter_class=formatter,
    )
    commands = parser.add_subparsers(dest="threads_command", required=True)

    sync = commands.add_parser(
        "sync", help="fetch threads and their linked threads", formatter_class=formatter
    )
    sync.add_argument("numbers", type=int, nargs="*")
    sync.add_argument(
        "--open-pulls",
        type=int,
        nargs="?",
        const=100,
        default=0,
        metavar="N",
        help="also index what the N most recently updated open pull requests change",
    )
    sync.add_argument(
        "--refresh-linked",
        action="store_true",
        help="also refetch linked threads already stored",
    )
    sync.add_argument("--quiet", action="store_true")
    _common(sync)
    sync.set_defaults(handler=_run_sync)

    show = commands.add_parser(
        "show", help="print the context of one thread", formatter_class=formatter
    )
    _serving(show)
    show.add_argument("--json", action="store_true")
    show.set_defaults(handler=_run_show)

    context = commands.add_parser(
        "context",
        help="write the context to a file before an agent starts",
        formatter_class=formatter,
    )
    _serving(context)
    context.add_argument("--output", default="THREAD_CONTEXT.md")
    context.set_defaults(handler=_run_context)

    events = commands.add_parser(
        "events", help="print raw events by id", formatter_class=formatter
    )
    events.add_argument("addresses", nargs="+", help="ids such as '#12/comment/345'")
    events.add_argument("--json", action="store_true")
    _common(events)
    events.set_defaults(handler=_run_events)


__all__ = ["add_threads_parser"]
