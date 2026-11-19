# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""``codenib threads agent``: an always-on agent for issues and pull requests."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from pathlib import Path


def _home(args: argparse.Namespace, checkout):
    from .agent import AgentHome
    from .cli import _book

    instructions = getattr(args, "instructions", None)
    return AgentHome(
        _book(args, checkout),
        Path(instructions).expanduser() if instructions else None,
    )


def _runner(args: argparse.Namespace):
    from .agent import ClaudeRunner, CommandRunner

    timeout = args.timeout * 60
    if args.agent_command:
        return CommandRunner(shlex.split(args.agent_command), timeout=timeout)
    return ClaudeRunner(
        model=args.model or "", max_budget_usd=args.max_budget_usd, timeout=timeout
    )


def _print_report(report) -> None:
    if report.polled:
        p = report.polled
        print(
            f"Polled: {p['threads_fetched']} threads fetched in {p['requests']} "
            f"requests, {p['new_events']} new events."
        )
    if report.opened:
        print("Now watching: " + ", ".join(f"#{n}" for n in report.opened))
    if not report.runs:
        print("Nothing needed the agent.")
    for run in report.runs:
        line = f"#{run.ticket}: {run.outcome}"
        if run.message:
            line += f" -> outbox {run.message}"
        if run.outcome not in ("skipped", "busy"):
            cost = f", ${run.cost_usd:.3f}" if run.cost_usd is not None else ""
            line += f" ({run.prompt_tokens} prompt tokens, {run.seconds:.0f}s{cost})"
        if run.error:
            line += f" [{run.error}]"
        print(line)
    if report.stuck:
        print(
            "Not retried after repeated failures: "
            + ", ".join(f"#{n}" for n in report.stuck)
            + " (see `codenib threads agent status`)"
        )
    if report.left_for_next_pass:
        print(
            "Left for the next pass: "
            + ", ".join(f"#{n}" for n in report.left_for_next_pass)
        )
    if report.closed:
        print("Closed and done: " + ", ".join(f"#{n}" for n in report.closed))
    if report.waiting:
        print(
            f"{len(report.waiting)} message(s) wait for a maintainer: "
            "codenib threads agent outbox"
        )


def _run_agent_pass(args: argparse.Namespace) -> int:
    from .agent import AgentBusy, WatchRule, run_pass
    from .cli import _checkout, _client, _error

    checkout = _checkout(args.checkout)
    if checkout is None:
        raise _error("run inside a clone of the repository, or pass --checkout")
    home = _home(args, checkout)
    rule = None
    if args.watch:
        rule = WatchRule(
            kinds=args.watch,
            labels=tuple(args.label or ()),
            lookback_days=args.lookback_days,
            max_new=args.max_new,
        )
    if not home.book.all() and rule is None:
        raise _error(
            "no tickets to work; pass --watch pulls (or issues, all), or open "
            "one with `codenib threads open N`"
        )
    client = None if args.offline else _client(args)
    runner = _runner(args)

    def one() -> int:
        try:
            report = run_pass(
                home,
                runner,
                checkout=checkout,
                client=client,
                rule=rule,
                worker=args.worker or "",
                max_runs=args.max_runs,
                skip_bot_posts=not args.include_bots,
                on_progress=(
                    None if args.quiet else (lambda m: print(f"  {m}", file=sys.stderr))
                ),
            )
        except AgentBusy as exc:
            print(f"{exc}; not starting a second one.", file=sys.stderr)
            return 3
        except RuntimeError as exc:
            raise _error(str(exc)) from exc
        if args.json:
            print(json.dumps(report.to_json(), indent=2))
        else:
            _print_report(report)
        return 1 if any(r.outcome == "failed" for r in report.runs) else 0

    if not args.every:
        return one()
    try:
        while True:
            one()
            sys.stdout.flush()
            time.sleep(args.every * 60)
    except KeyboardInterrupt:
        return 0


def _show_message(message) -> None:
    label = {
        "draft": "reply draft",
        "question": "question for the maintainers",
    }.get(message.status, message.status)
    print(f"== {message.id}  #{message.ticket}  {label}  ({message.created})")
    if message.reason:
        print(f"   why: {message.reason}")
    print(message.body)
    if message.answer:
        print(f"   answer: {message.answer}")
    if message.comment_url:
        print(f"   posted: {message.comment_url}")
    print()


def _run_outbox(args: argparse.Namespace) -> int:
    from .cli import _checkout

    home = _home(args, _checkout(args.checkout))
    messages = home.outbox.all() if args.all else home.outbox.waiting()
    if args.json:
        print(json.dumps([m.to_json() for m in messages], indent=2))
        return 0
    if not messages:
        print("Nothing waits in the outbox.")
        return 0
    for message in messages:
        _show_message(message)
    waiting = [m for m in messages if m.waiting]
    if waiting:
        print(
            "Send a reply with `codenib threads agent send ID`, drop it with "
            "`discard ID`, answer a question with `answer ID TEXT`."
        )
    return 0


def _run_send(args: argparse.Namespace) -> int:
    from .cli import _checkout, _error
    from .github_source import default_api_url, resolve_token
    from .outbox import SendError, send

    if bool(args.ids) == bool(args.all):
        raise _error("name the drafts to send, or pass --all")
    home = _home(args, _checkout(args.checkout))
    if args.all:
        messages = [m for m in home.outbox.waiting() if m.status == "draft"]
    else:
        messages = []
        for ident in args.ids:
            message = home.outbox.get(ident)
            if message is None:
                raise _error(f"no message {ident} in the outbox")
            messages.append(message)
    if not messages:
        print("No reply drafts to send.")
        return 0
    token = resolve_token()
    if not token:
        raise _error(
            "sending needs a GitHub token that can comment: set GH_TOKEN, or "
            "run `gh auth login`"
        )
    api_url = args.api_url or default_api_url()
    for message in messages:
        try:
            sent = send(
                home.outbox, message, repo=home.store.repo, token=token, api_url=api_url
            )
        except SendError as exc:
            raise _error(str(exc)) from exc
        print(f"Sent {sent.id} to #{sent.ticket}: {sent.comment_url}")
    return 0


def _run_discard(args: argparse.Namespace) -> int:
    from .cli import _checkout, _error

    home = _home(args, _checkout(args.checkout))
    for ident in args.ids:
        message = home.outbox.get(ident)
        if message is None:
            raise _error(f"no message {ident} in the outbox")
        if not message.waiting:
            raise _error(f"{ident} is {message.status}; only a waiting one can go")
        message.status = "discarded"
        home.outbox.put(message)
        print(
            f"Discarded {ident}. The agent sees that on its next run on #{message.ticket}."
        )
    return 0


def _run_answer(args: argparse.Namespace) -> int:
    from .cli import _checkout, _error

    home = _home(args, _checkout(args.checkout))
    try:
        message = home.answer(args.id, args.text)
    except KeyError as exc:
        raise _error(f"no message {args.id} in the outbox") from exc
    except ValueError as exc:
        raise _error(str(exc)) from exc
    print(f"Answered. The next pass wakes the agent on #{message.ticket} with it.")
    return 0


def _run_status(args: argparse.Namespace) -> int:
    from .cli import _checkout
    from .tickets import live_claim, pending

    home = _home(args, _checkout(args.checkout))
    runs = home.runs()
    timers = home.timers()
    rows = []
    for ticket in home.book.all():
        state = home.state(ticket.number)
        header = home.store.header(ticket.number)
        rows.append(
            {
                "number": ticket.number,
                "title": header.title if header else "",
                "unread": pending(home.book, ticket),
                "read_in_full": bool(state.get("briefed")),
                "opened_by_agent": bool(state.get("opened_by_agent")),
                "last_run": state.get("last_run", ""),
                "last_outcome": state.get("last_outcome", ""),
                "failures": int(state.get("failures") or 0),
                "last_error": state.get("last_error", ""),
                "follow_up": timers.get(str(ticket.number)),
                "claim": live_claim(ticket) or None,
                "notes_words": len(home.notes(ticket.number).split()),
            }
        )
    worked = [r for r in runs if r.get("outcome") not in ("skipped", "busy")]
    costs = [r["cost_usd"] for r in worked if r.get("cost_usd") is not None]
    totals = {
        "runs": len(worked),
        "skipped_without_a_run": sum(1 for r in runs if r.get("outcome") == "skipped"),
        "failed": sum(1 for r in worked if r.get("outcome") == "failed"),
        "prompt_tokens_mean": (
            round(sum(r.get("prompt_tokens", 0) for r in worked) / len(worked))
            if worked
            else 0
        ),
        "cost_usd": round(sum(costs), 4) if costs else None,
        "waiting_in_outbox": len(home.outbox.waiting()),
        "instructions": str(home.instructions_path),
        "instructions_present": home.instructions_path.exists(),
    }
    if args.json:
        print(json.dumps({"tickets": rows, "totals": totals}, indent=2))
        return 0
    if not rows:
        print("No tickets.")
    for row in rows:
        bits = [f"{row['unread']} unread" if row["unread"] else "up to date"]
        if not row["read_in_full"]:
            bits.append("not read yet")
        if row["last_outcome"]:
            bits.append(f"last: {row['last_outcome']} {row['last_run']}")
        if row["follow_up"]:
            bits.append(f"follow-up {row['follow_up']['due']}")
        if row["failures"]:
            bits.append(f"{row['failures']} failed: {row['last_error']}")
        print(f"#{row['number']} [{', '.join(bits)}] {row['title'] or ''}")
    cost = (
        f", ${totals['cost_usd']:.3f} recorded"
        if totals["cost_usd"] is not None
        else ""
    )
    print(
        f"\n{totals['runs']} runs ({totals['failed']} failed), "
        f"{totals['skipped_without_a_run']} wakes needed no run, "
        f"{totals['prompt_tokens_mean']} prompt tokens per run{cost}; "
        f"{totals['waiting_in_outbox']} waiting in the outbox."
    )
    where = "" if totals["instructions_present"] else " (none yet)"
    print(f"Standing instructions: {totals['instructions']}{where}")
    return 0


def add_agent_parser(commands, formatter) -> None:
    from .agent import DEFAULT_MAX_RUNS
    from .cli import _common

    agent = commands.add_parser(
        "agent",
        help="run an always-on agent on the repository's issues and pull "
        "requests; it drafts, a maintainer sends",
        formatter_class=formatter,
    )
    steps = agent.add_subparsers(dest="agent_command", required=True)

    run = steps.add_parser(
        "run",
        help="wake the agent once: poll, run it where something changed, file "
        "its drafts",
        formatter_class=formatter,
    )
    run.add_argument(
        "--watch",
        choices=("pulls", "issues", "all"),
        help="also start a ticket for each open thread of this kind that changes",
    )
    run.add_argument(
        "--label", action="append", help="watch only threads with this label"
    )
    run.add_argument(
        "--lookback-days",
        type=float,
        default=3.0,
        help="on the first pass, watch threads updated this recently",
    )
    run.add_argument(
        "--max-new", type=int, default=5, help="tickets to start per pass at most"
    )
    run.add_argument(
        "--max-runs",
        type=int,
        default=DEFAULT_MAX_RUNS,
        help="agent runs per pass at most; the rest wait for the next pass",
    )
    run.add_argument("--model", help="model for Claude Code; unset, its own default")
    run.add_argument(
        "--max-budget-usd",
        type=float,
        default=0.5,
        help="spending limit of one Claude Code run",
    )
    run.add_argument(
        "--timeout", type=int, default=10, metavar="MINUTES", help="limit of one run"
    )
    run.add_argument(
        "--agent-command",
        help="another headless agent instead of Claude Code: it reads the prompt "
        "on standard input and prints a JSON decision",
    )
    run.add_argument(
        "--instructions",
        help="standing instructions file; unset, agent/INSTRUCTIONS.md in the " "store",
    )
    run.add_argument(
        "--worker", help="name this worker claims under; unset, agent@host"
    )
    run.add_argument(
        "--include-bots",
        action="store_true",
        help="also wake the agent when the only news is bot comments",
    )
    run.add_argument(
        "--every",
        type=float,
        metavar="MINUTES",
        help="keep running, one pass every MINUTES; unset, one pass",
    )
    run.add_argument("--offline", action="store_true", help="never call GitHub")
    run.add_argument("--quiet", action="store_true")
    run.add_argument("--json", action="store_true")
    _common(run)
    run.set_defaults(handler=_run_agent_pass)

    outbox = steps.add_parser(
        "outbox",
        help="show the replies and questions waiting for a maintainer",
        formatter_class=formatter,
    )
    outbox.add_argument("--all", action="store_true", help="also sent and dropped")
    outbox.add_argument("--json", action="store_true")
    _common(outbox)
    outbox.set_defaults(handler=_run_outbox)

    sending = steps.add_parser(
        "send",
        help="post reply drafts as comments; the only step that writes to GitHub",
        formatter_class=formatter,
    )
    sending.add_argument("ids", nargs="*", help="outbox ids")
    sending.add_argument("--all", action="store_true", help="every reply draft")
    _common(sending)
    sending.set_defaults(handler=_run_send)

    discard = steps.add_parser(
        "discard", help="drop waiting drafts or questions", formatter_class=formatter
    )
    discard.add_argument("ids", nargs="+")
    _common(discard)
    discard.set_defaults(handler=_run_discard)

    answer = steps.add_parser(
        "answer",
        help="answer the agent's question; the next pass wakes it with the answer",
        formatter_class=formatter,
    )
    answer.add_argument("id")
    answer.add_argument("text")
    _common(answer)
    answer.set_defaults(handler=_run_answer)

    status = steps.add_parser(
        "status",
        help="tickets, follow-ups, failures and what the runs cost",
        formatter_class=formatter,
    )
    status.add_argument("--json", action="store_true")
    _common(status)
    status.set_defaults(handler=_run_status)


__all__ = ["add_agent_parser"]
