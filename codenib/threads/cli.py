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


def _checkout(value: str | None, *, required: bool = True) -> Path | None:
    """The clone to resolve code against; ``None`` when there is none.

    A path the user named with --checkout must be a repository. The current
    directory, or a ticket's work directory, is used only if it is one.
    """

    path = Path(value or ".").expanduser().resolve()
    proc = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        if value and required:
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


def _client(args: argparse.Namespace):
    from .github_source import connect

    try:
        return connect(getattr(args, "api_url", None))
    except ValueError as exc:
        raise _error(str(exc)) from exc


def _store(args: argparse.Namespace, checkout: Path | None):
    from .log_store import ThreadStore

    root = Path(args.store).expanduser() if args.store else None
    try:
        return ThreadStore(_repo_name(args, checkout), root)
    except ValueError as exc:
        raise _error(str(exc)) from exc


def _sync(
    args, store, numbers: list[int], *, refresh_linked: bool, quiet: bool
) -> None:
    from .sync import sync_thread

    client = _client(args)
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


def _sync_open_pulls(args, store, limit: int, quiet: bool) -> None:
    from .in_flight import fetch_open_pulls, write_index

    try:
        pulls = fetch_open_pulls(_client(args), store.repo, limit)
    except RuntimeError as exc:
        raise _error(str(exc)) from exc
    write_index(store, pulls)
    if not quiet:
        print(f"indexed {len(pulls)} open pull requests", file=sys.stderr)


def _run_sync(args: argparse.Namespace) -> int:
    if not args.numbers and not args.open_pulls:
        raise _error("name a thread number, or pass --open-pulls")
    store = _store(args, _checkout(args.checkout))
    _sync(
        args,
        store,
        args.numbers,
        refresh_linked=args.refresh_linked,
        quiet=args.quiet,
    )
    if args.open_pulls:
        _sync_open_pulls(args, store, args.open_pulls, args.quiet)
    return 0


def _packet(args: argparse.Namespace):
    from .in_flight import read_index
    from .serve import build_packet, load_bundle

    checkout = _checkout(args.checkout)
    store = _store(args, checkout)
    if not args.offline and (args.sync or not store.has(args.number)):
        _sync(args, store, [args.number], refresh_linked=args.sync, quiet=True)
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


def _book(args: argparse.Namespace, checkout: Path | None):
    from .tickets import TicketBook

    return TicketBook(_store(args, checkout))


def _ticket(book, number: int):
    ticket = book.get(number)
    if ticket is None:
        raise _error(
            f"no ticket for #{number}; start one with `codenib threads open {number}`"
        )
    return ticket


def _emit(args: argparse.Namespace, packet, directory: str) -> None:
    if getattr(args, "json", False):
        print(json.dumps(packet.to_json(), indent=2))
    else:
        _write(args, packet.text, directory)


def _write(args: argparse.Namespace, text: str, directory: str) -> None:
    if args.output or directory:
        target = Path(args.output or Path(directory) / "THREAD_CONTEXT.md").expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"Wrote {target}", file=sys.stderr)
    else:
        sys.stdout.write(text)


def _run_open(args: argparse.Namespace) -> int:
    from .in_flight import read_index
    from .tickets import open_ticket

    if args.checkout:
        checkout = _checkout(args.checkout)
    else:
        checkout = _checkout(args.dir, required=False) or _checkout(None)
    book = _book(args, checkout)
    if not args.offline and (args.sync or not book.store.has(args.number)):
        _sync(
            args,
            book.store,
            [args.number, *args.follow],
            refresh_linked=args.sync,
            quiet=True,
        )
    opened = open_ticket(
        book,
        args.number,
        directory=str(Path(args.dir).expanduser().resolve()) if args.dir else "",
        follow=args.follow,
        budget=args.budget,
        code_budget=args.code_budget,
        checkout=None if args.no_anchors else checkout,
        rev=args.rev,
        open_pulls=read_index(book.store),
    )
    if opened is None:
        raise _error(f"{book.store.repo}#{args.number} is not in the store")
    ticket, packet = opened
    _emit(args, packet, ticket.directory)
    follows = ", ".join(f"#{n}" for n in ticket.threads)
    print(f"Ticket #{ticket.number} follows {follows}", file=sys.stderr)
    return 0


def _run_inbox(args: argparse.Namespace) -> int:
    from .tickets import read_inbox

    checkout = _checkout(args.checkout)
    book = _book(args, checkout)
    ticket = _ticket(book, args.number)
    if args.sync:
        _sync(args, book.store, ticket.threads, refresh_linked=False, quiet=True)
    if checkout is None and ticket.directory:
        checkout = _checkout(ticket.directory, required=False)
    packet = read_inbox(
        book,
        ticket,
        budget=args.budget,
        code_budget=args.code_budget,
        checkout=None if args.no_anchors else checkout,
        rev=args.rev,
        advance=not args.peek,
    )
    if packet is None:
        print(f"Nothing new for ticket #{ticket.number}.", file=sys.stderr)
        return 0
    _emit(args, packet, "")
    return 0


def _run_poll(args: argparse.Namespace) -> int:
    from .poll import poll

    book = _book(args, _checkout(args.checkout))
    if not book.all():
        raise _error("no tickets; start one with `codenib threads open N`")
    try:
        result = poll(_client(args), book)
    except RuntimeError as exc:
        raise _error(str(exc)) from exc
    if args.json:
        print(json.dumps(result.__dict__, indent=2))
        return 0
    print(
        f"{len(result.refetched)} threads fetched in {result.requests} requests; "
        f"{sum(result.new_events.values())} new events"
    )
    for number in result.tickets_with_news:
        print(f"  ticket #{number} has news: codenib threads inbox {number}")
    return 0


def _run_board(args: argparse.Namespace) -> int:
    from .in_flight import read_index
    from .tickets import board

    book = _book(args, _checkout(args.checkout))
    rows = board(book, read_index(book.store))
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "number": r.ticket.number,
                        "title": r.title,
                        "state": r.state,
                        "unread": r.unread,
                        "follows": r.ticket.threads,
                        "shares_code_with": r.shares_code_with,
                        "open_pulls_on_same_symbols": r.open_pulls,
                    }
                    for r in rows
                ],
                indent=2,
            )
        )
        return 0
    if not rows:
        print("No tickets.")
        return 0
    for row in rows:
        unread = f"{row.unread} unread" if row.unread else "up to date"
        print(
            f"#{row.ticket.number} [{row.state}, {unread}, follows "
            f"{len(row.ticket.threads)} threads] {row.title}"
        )
        for other, symbols in sorted(row.shares_code_with.items()):
            names = ", ".join(f"`{s}`" for s in dict.fromkeys(symbols))
            print(f"    same code as ticket #{other}: {names}")
        for number, symbols in sorted(row.open_pulls.items())[:8]:
            names = ", ".join(f"`{s}`" for s in symbols[:3])
            print(f"    open pull request #{number} changes {names}")
    return 0


def _run_mcp(args: argparse.Namespace) -> int:
    from .mcp_server import build_server

    checkout = _checkout(args.checkout)
    root = Path(args.store).expanduser() if args.store else None
    try:
        server = build_server(
            _repo_name(args, checkout), root, checkout, api_url=args.api_url
        )
    except ImportError as exc:
        raise _error(
            "the MCP server needs the mcp package: pip install 'codenib[mcp]'"
        ) from exc
    server.run(transport="stdio")
    return 0


def _run_close(args: argparse.Namespace) -> int:
    book = _book(args, _checkout(args.checkout))
    removed = book.remove(args.number)
    print(f"Ticket #{args.number} " + ("closed." if removed else "did not exist."))
    return 0


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", help="owner/name; default: the origin remote")
    parser.add_argument(
        "--checkout", help="local clone used to resolve code locations (default: .)"
    )
    parser.add_argument(
        "--store", help="thread store directory (default: CodeNib home)"
    )
    parser.add_argument(
        "--api-url",
        help="GitHub REST API root, for GitHub Enterprise "
        "(default: $GITHUB_API_URL, else https://api.github.com)",
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

    opened = commands.add_parser(
        "open",
        help="start a ticket on a thread and write its first context",
        formatter_class=formatter,
    )
    _serving(opened)
    opened.add_argument("--dir", help="the ticket's work directory")
    opened.add_argument(
        "--follow", type=int, nargs="*", default=[], help="other threads to follow"
    )
    opened.add_argument("--output", help="file to write (default: stdout, or --dir)")
    opened.add_argument("--json", action="store_true")
    opened.set_defaults(handler=_run_open)

    inbox = commands.add_parser(
        "inbox",
        help="print what a ticket's threads gained since it last read",
        formatter_class=formatter,
    )
    _serving(inbox)
    inbox.add_argument("--peek", action="store_true", help="do not mark as read")
    inbox.add_argument("--output", help="file to write (default: stdout)")
    inbox.add_argument("--json", action="store_true")
    inbox.set_defaults(handler=_run_inbox)

    polling = commands.add_parser(
        "poll",
        help="fetch what changed on the threads tickets follow",
        formatter_class=formatter,
    )
    polling.add_argument("--json", action="store_true")
    _common(polling)
    polling.set_defaults(handler=_run_poll)

    boards = commands.add_parser(
        "board",
        help="list tickets, their unread events, and tickets on the same code",
        formatter_class=formatter,
    )
    boards.add_argument("--json", action="store_true")
    _common(boards)
    boards.set_defaults(handler=_run_board)

    closing = commands.add_parser(
        "close", help="stop following a ticket's threads", formatter_class=formatter
    )
    closing.add_argument("number", type=int)
    _common(closing)
    closing.set_defaults(handler=_run_close)

    mcp = commands.add_parser(
        "mcp",
        help="serve thread context and tickets over the Model Context Protocol",
        formatter_class=formatter,
    )
    _common(mcp)
    mcp.set_defaults(handler=_run_mcp)

    events = commands.add_parser(
        "events", help="print raw events by id", formatter_class=formatter
    )
    events.add_argument("addresses", nargs="+", help="ids such as '#12/comment/345'")
    events.add_argument("--json", action="store_true")
    _common(events)
    events.set_defaults(handler=_run_events)


__all__ = ["add_threads_parser"]
