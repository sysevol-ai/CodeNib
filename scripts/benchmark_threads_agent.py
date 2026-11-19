#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Replay merged pull requests through an always-on agent, two ways.

Each pull request is replayed from the moment it was opened to its merge.
Every 15 minutes of its history is one pass. Before a pass, the replay store
receives exactly the events that existed then. The agent works on a snapshot
of the repository at the pull request's base, with no history, and the diff
it is shown is the diff of the head commit at that time. So no arm can see a
later commit, review or merge.

Arms differ only in how the agent is woken and what it is handed:

* ``codenib``  ``codenib threads agent`` as shipped: it wakes only for news
  that can call for a response, the first run reads the whole context, later
  runs read only the news, its own note on the thread and what it already
  said.
* ``codenib-commits`` the same, plus the rule that a run after new commits
  is handed what they changed and asked to review it. Its first run is the
  ``codenib`` arm's first run, whose input is identical, so the two differ
  only in what the rule changes. The rule was tested on November pull
  requests and not shipped; this arm needs the ``experiment/review-new-commits``
  branch (753ae262), where ``run_ticket`` takes ``review_new_commits``.
* ``stateless`` wakes for the same news, but reads the whole prepared context
  every time and keeps no memory. The context is CodeNib's own budgeted
  packet, so the comparison leaves out what preparing the context saves.
* ``naive``    stateless, and woken whenever the thread changed at all. Run on
  the pilot only, to measure what the wake filter saves.

Only "clean" pull requests are replayed: never force-pushed, not stacked on
another branch, base never changed. For those, a commit's committer date is
when it was pushed, so the replay knows what the pull request contained at
every moment.

Both arms run the same Claude Code command, model, tools, system prompt and
decision schema. Nothing is posted.

    benchmark_threads_agent.py select --out O [--merged 2026-10-01..2026-10-31]
    benchmark_threads_agent.py refine --out O
    benchmark_threads_agent.py fetch  --out O --reference CLONE
    benchmark_threads_agent.py replay --out O --arm codenib [--limit N]
    benchmark_threads_agent.py score  --out O
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import random
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from codenib.threads import agent as agent_mod
from codenib.threads.agent import (
    AgentHome,
    ClaudeRunner,
    news_for_agent,
    run_prompt,
    run_ticket,
    system_prompt,
    write_pull_diff,
)
from codenib.threads.events import ThreadEvent, event_address
from codenib.threads.log_store import ThreadStore
from codenib.threads.serve import (
    build_packet,
    count_tokens,
    linked_numbers,
    load_bundle,
)
from codenib.threads.tickets import TicketBook, open_ticket

REPO = "astral-sh/ruff"
PASS_MINUTES = 15
MAX_RUNS_PER_PULL = 30
SEED = 20261119
PILOT = 4
MAX_FILES = 30
MAX_LINES = 1500
MAX_DAYS = 14
# The standing instructions a maintainer gives a pull request agent, the same
# text as in the live run on CodeNib's own repository. Both arms get them.
INSTRUCTIONS = (
    "- For a pull request, check the change against the code it touches: say "
    "whether it does what it claims, and name any case or test it misses. Cite "
    "file:line.\n"
    "- Outside contributors are welcome; thank them once, then be specific.\n"
    "- Do not restate the pull request description.\n"
)
# Round 3: the same, widened to what reviewers mostly comment on (design,
# reuse, placement). Fixed before any run on the repositories it was tried on.
INSTRUCTIONS_BROAD = (
    "- For a pull request, review it as a maintainer of this repository would. "
    "Cite file:line.\n"
    "  - Correctness: whether it does what it claims, and any case it misses.\n"
    "  - Reuse: search the code for an existing helper, type or pattern that the "
    "new code duplicates or should use, and name it.\n"
    "  - Placement: whether code or tests belong elsewhere, judged by where similar "
    "code and tests already live.\n"
    "  - Tests: whether the tests cover the change, and any case they miss.\n"
    "  - Small points only when a maintainer would raise them: naming, a simpler "
    "form, a misleading comment.\n"
    "- Outside contributors are welcome; thank them once, then be specific.\n"
    "- Do not restate the pull request description.\n"
)
BOT_RE = re.compile(r"\[bot\]$|^(renovate|dependabot|github-actions)", re.I)


def base_ref(git: Path) -> str:
    """The default branch as fetched: origin/base, or origin/main for older runs."""

    found = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", "refs/remotes/origin/base"],
        cwd=git, capture_output=True,
    )  # fmt: skip
    return (
        "refs/remotes/origin/base"
        if found.returncode == 0
        else "refs/remotes/origin/main"
    )


def sh(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:4])}: {proc.stderr.strip()[-400:]}")
    return proc.stdout


def parse(at: str) -> datetime:
    return datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ceil_pass(at: str) -> str:
    moment = parse(at)
    step = PASS_MINUTES * 60
    seconds = int(moment.timestamp())
    return iso(datetime.fromtimestamp(-(-seconds // step) * step, timezone.utc))


def is_human(login: str, is_bot: bool) -> bool:
    return not is_bot and not BOT_RE.search(login or "")


# -------------------------------------------------------------------- select


def select(args: argparse.Namespace) -> int:
    from codenib.threads.github_source import connect

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    client = connect()
    query = f"repo:{args.repo} is:pr is:merged merged:{args.merged}"
    items = client.pages(
        "search/issues?q=" + urllib.parse.quote(query) + "&sort=created&order=asc",
        key="items",
        max_pages=10,
    )
    # Check candidates in a seeded random order, so stopping early does not
    # favour one end of the window.
    items = sorted(items, key=lambda item: item["number"])
    random.Random(args.seed).shuffle(items)
    rows = []
    for item in items:
        if (
            args.max_eligible
            and sum("excluded" not in r for r in rows) >= args.max_eligible
        ):
            break
        number = item["number"]
        author = (item.get("user") or {}).get("login") or ""
        row = {"number": number, "author": author, "title": item.get("title", "")}
        rows.append(row)
        if not is_human(author, (item.get("user") or {}).get("type") == "Bot"):
            row["excluded"] = "bot author"
            continue
        pull = client.get(f"repos/{args.repo}/pulls/{number}") or {}
        row.update(
            files=pull.get("changed_files", 0),
            lines=pull.get("additions", 0) + pull.get("deletions", 0),
            created=pull.get("created_at", ""),
            merged=pull.get("merged_at", ""),
        )
        if row["files"] > MAX_FILES or row["lines"] > MAX_LINES:
            row["excluded"] = "too large"
            continue
        if not row["merged"] or parse(row["merged"]) - parse(
            row["created"]
        ) > timedelta(days=MAX_DAYS):
            row["excluded"] = "open too long"
            continue
        comments = client.pages(f"repos/{args.repo}/pulls/{number}/comments")
        human = [
            c
            for c in comments
            if (c.get("user") or {}).get("login") != author
            and is_human(
                (c.get("user") or {}).get("login", ""),
                (c.get("user") or {}).get("type") == "Bot",
            )
        ]
        row["human_review_comments"] = len(human)
        if not human:
            row["excluded"] = "no inline review by someone else"
    eligible = sorted(r["number"] for r in rows if "excluded" not in r)
    order = list(eligible)
    random.Random(args.seed).shuffle(order)
    chosen = order[: args.n]
    selection = {
        "repo": args.repo,
        "branch": (client.get(f"repos/{args.repo}") or {}).get(
            "default_branch", "main"
        ),
        "merged": args.merged,
        "seed": args.seed,
        "criteria": {
            "max_files": MAX_FILES,
            "max_lines": MAX_LINES,
            "max_days_open": MAX_DAYS,
            "needs": "an inline review comment by a person other than the author",
        },
        "candidates": len(rows),
        "eligible": len(eligible),
        "order": chosen,
        "pilot": chosen[:PILOT],
        "rows": rows,
        "requests": client.requests,
    }
    (out / "selection.json").write_text(json.dumps(selection, indent=1))
    print(
        f"{len(rows)} merged pull requests, {len(eligible)} eligible, "
        f"{len(chosen)} chosen ({client.requests} requests)"
    )
    return 0


UNCLEAN = frozenset(
    {
        "head_ref_force_pushed",
        "base_ref_force_pushed",
        "base_ref_changed",
        "added_to_stack",
        "removed_from_stack",
    }
)


def refine(args: argparse.Namespace) -> int:
    """Keep the eligible pull requests whose history can be replayed exactly."""

    from codenib.threads.github_source import connect
    from codenib.threads.sync import sync_thread

    out = Path(args.out)
    selection = json.loads((out / "selection.json").read_text())
    store = ThreadStore(selection["repo"], out / "store")
    client = connect()
    eligible = sorted(r["number"] for r in selection["rows"] if "excluded" not in r)
    clean, reasons = [], {}
    for number in eligible:
        if not store.has(number):
            sync_thread(client, store, number)
        kinds = {e.kind for e in store.events(number)}
        found = sorted(kinds & UNCLEAN)
        if found:
            reasons[str(number)] = found
        else:
            clean.append(number)
    order = list(clean)
    random.Random(selection["seed"]).shuffle(order)
    selection.update(
        eligible_order_before_refine=selection["order"],
        clean=len(clean),
        not_clean=reasons,
        order=order[: args.n],
        pilot=order[:PILOT],
        refine_requests=client.requests,
    )
    (out / "selection.json").write_text(json.dumps(selection, indent=1))
    print(
        f"{len(clean)} of {len(eligible)} eligible are clean; "
        f"{len(selection['order'])} chosen ({client.requests} requests)"
    )
    return 0


# --------------------------------------------------------------------- fetch


def fetch(args: argparse.Namespace) -> int:
    from codenib.threads.github_source import connect
    from codenib.threads.sync import sync_thread

    out = Path(args.out)
    selection = json.loads((out / "selection.json").read_text())
    repo = selection["repo"]
    numbers = selection["order"][: args.limit] if args.limit else selection["order"]
    store = ThreadStore(repo, out / "store")
    client = connect()
    for number in numbers:
        if not store.has(number):
            sync_thread(client, store, number)
    print(f"threads fetched ({client.requests} requests)")

    git = out / "git"
    if not git.exists():
        reference = ["--reference", args.reference] if args.reference else []
        sh(
            "git", "clone", "-q", "--no-checkout", *reference,
            f"https://github.com/{repo}.git", str(git),
        )  # fmt: skip
    branch = selection.get("branch", "main")
    refspecs = [f"+refs/pull/{n}/head:refs/pr/{n}" for n in numbers]
    sh("git", "fetch", "-q", "origin",
       f"+refs/heads/{branch}:refs/remotes/origin/base", *refspecs, cwd=git)  # fmt: skip

    meta = {}
    for number in numbers:
        # Commits since the pull request forked from the default branch as it
        # stood when it opened. This holds for squash merges and for merge
        # commits alike, where the commits end up on the default branch.
        opened = store.header(number).created_at
        head = sh("git", "rev-parse", f"refs/pr/{number}", cwd=git).strip()
        main_then = sh("git", "rev-list", "-1", f"--before={opened}",
                       base_ref(git), cwd=git).strip()  # fmt: skip
        base = sh("git", "merge-base", main_then, head, cwd=git).strip()
        # First parents only: commits that came in by merging the default
        # branch into the pull request are not heads it ever had.
        commits = sh(
            "git", "rev-list", "--reverse", "--first-parent", f"{base}..{head}",
            cwd=git,
        ).split()  # fmt: skip
        if not commits:
            meta[str(number)] = {"skip": "no commits after the fork point"}
            continue
        dated = []
        for sha in commits:
            stamp = sh("git", "show", "-s", "--format=%cI", sha, cwd=git).strip()
            dated.append([sha, iso(datetime.fromisoformat(stamp))])
        snapshot = out / "snap" / str(number)
        if not snapshot.exists():
            snapshot.mkdir(parents=True)
            archive = subprocess.Popen(
                ["git", "archive", base], cwd=git, stdout=subprocess.PIPE
            )
            subprocess.run(["tar", "-x", "-C", str(snapshot)], stdin=archive.stdout,
                           check=True)  # fmt: skip
            archive.wait()
        meta[str(number)] = {"base": base, "commits": dated}
    (out / "pulls.json").write_text(json.dumps(meta, indent=1))
    print(f"{sum('base' in m for m in meta.values())} pull requests ready")
    return 0


# -------------------------------------------------------------------- replay


class ReplayGitHub:
    """Serves the one request the agent's pass makes: a pull request's files,
    as of the head commit the replay is at."""

    requests = 0

    def __init__(self, git: Path, base: str) -> None:
        self.git = git
        self.base = base
        self.head = ""

    def pages(self, path: str, key: str | None = None, max_pages: int = 30) -> list:
        if not path.endswith("/files") or not self.head:
            return []
        return diff_files(self.git, self.base, self.head)

    def get(self, path: str):
        match = re.search(r"/compare/([0-9a-f]+)\.\.\.([0-9a-f]+)$", path)
        if not match:
            return None
        old, new = match.groups()
        ahead = (
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", old, new], cwd=self.git
            ).returncode
            == 0
        )
        return {
            "status": "ahead" if ahead else "diverged",
            "files": diff_files(self.git, old, new),
        }


def diff_base(git: Path, head: str, t: str) -> str:
    """Where GitHub's diff of ``head`` started at ``t``: its merge base with main."""

    main_then = sh("git", "rev-list", "-1", f"--before={t}",
                   base_ref(git), cwd=git).strip()  # fmt: skip
    return sh("git", "merge-base", main_then, head, cwd=git).strip()


def diff_files(git: Path, base: str, head: str) -> list[dict]:
    numstat = {}
    for line in sh("git", "diff", "--numstat", "-M", base, head, cwd=git).splitlines():
        added, removed, name = line.split("\t", 2)
        if " => " in name:
            name = re.sub(r"\{(.*) => (.*)\}", r"\2", name).split(" => ")[-1]
        numstat[name] = (
            int(added) if added.isdigit() else 0,
            int(removed) if removed.isdigit() else 0,
        )
    files = []
    text = sh("git", "diff", "-M", "--no-color", base, head, cwd=git)
    for section in re.split(r"^(?=diff --git )", text, flags=re.M):
        if not section.startswith("diff --git "):
            continue
        header, _, rest = section.partition("\n@@")
        match = re.match(r"diff --git a/(\S+) b/(\S+)", header)
        if not match:
            continue
        before, name = match.groups()
        added, removed = numstat.get(name, (0, 0))
        files.append(
            {
                "filename": name,
                "previous_filename": before if before != name else None,
                "status": "removed" if "deleted file mode" in header else "modified",
                "additions": added,
                "deletions": removed,
                "patch": ("@@" + rest).rstrip("\n") if rest else None,
            }
        )
    return files


class Spend:
    def __init__(self, cap: float) -> None:
        self.cap = cap
        self.total = 0.0
        self.lock = threading.Lock()

    def add(self, usd: float | None) -> None:
        with self.lock:
            self.total += usd or 0.0

    def exhausted(self) -> bool:
        with self.lock:
            return self.total >= self.cap


def _feed(full: ThreadStore, store: ThreadStore, number: int, pulls: dict, t: str,
          git: Path, refs: list[int]) -> str:  # fmt: skip
    """Bring the replay store to time ``t``; returns the diff's base then."""

    info = pulls[str(number)]
    head = ""
    for sha, visible in info["commits"]:
        if visible <= t:
            head = sha
    head = head or info["commits"][0][0]
    base = diff_base(git, head, t)
    header = full.header(number)
    events = [e for e in full.events(number) if e.at <= t and e.kind != "diff"]
    merged = any(e.kind == "merged" for e in events)
    links = {
        k: v
        for k, v in header.links.items()
        if int(k) in refs and (full.header(int(k)).created_at <= t)
    }
    store.write_header(
        dataclasses.replace(
            header,
            head_sha=head,
            base_sha=base,
            state="merged" if merged else "open",
            links=links,
            closed_at=header.closed_at if merged else "",
        )
    )
    files = diff_files(git, base, head)
    visible = dict(info["commits"]).get(head, t)
    diff = ThreadEvent(
        event_address(full.repo, number, "diff", head),
        number,
        "diff",
        "git",
        min(visible, t),
        True,
        "",
        {
            "sha": head,
            "files": [f["filename"] for f in files[:100]],
            "additions": sum(f["additions"] for f in files),
            "deletions": sum(f["deletions"] for f in files),
        },
    )
    arrived = store.append(number, events + [diff])
    for ref in refs:
        linked = full.header(ref)
        if linked is None or linked.created_at > t:
            continue
        store.write_header(linked)
        arrived += store.append(ref, [e for e in full.events(ref) if e.at <= t])
    return base


def replay_pull(out: Path, arm: str, number: int, args, spend: Spend) -> dict:
    # "codenib.r2" is a repeat of the codenib arm, kept in its own directory.
    kind = arm.split(".")[0]
    pulls = json.loads((out / "pulls.json").read_text())
    info = pulls.get(str(number), {})
    if "base" not in info:
        return {"number": number, "skipped": info.get("skip", "not fetched")}
    repo = json.loads((out / "selection.json").read_text())["repo"]
    full = ThreadStore(repo, out / "store")
    header = full.header(number)
    events = full.events(number)
    opened = header.created_at
    merged_at = next(e.at for e in events if e.kind == "merged")
    refs = [r for r in linked_numbers(header, events) if full.has(r)]
    work = out / "runs" / arm / str(number)
    if work.exists():
        shutil.rmtree(work)
    store = ThreadStore(repo, work / "store")
    home = AgentHome(TicketBook(store))
    home.instructions_path.parent.mkdir(parents=True, exist_ok=True)
    home.instructions_path.write_text(
        INSTRUCTIONS_BROAD if kind == "codenib-broad" else INSTRUCTIONS
    )
    git = out / "git"
    snapshot = out / "snap" / str(number)
    github = ReplayGitHub(git, info["base"])
    runner = ClaudeRunner(
        model=args.model, max_budget_usd=args.max_budget_usd, timeout=args.timeout * 60
    )
    if kind == "codenib-commits":
        first = out / "results" / "codenib" / f"{number}.json"
        if not first.exists():
            raise RuntimeError(
                "run the codenib arm first; this arm shares its first run"
            )
        runs_before = [
            r for r in json.loads(first.read_text())["records"]
            if r.get("outcome") != "skipped"
        ]  # fmt: skip
        if runs_before:
            runner = SharedFirstRun(runner, runs_before[0])
    stamps = {ceil_pass(opened)}
    for view_events in [events] + [full.events(r) for r in refs]:
        stamps |= {ceil_pass(e.at) for e in view_events if opened <= e.at <= merged_at}
    queue = sorted(s for s in stamps if s <= ceil_pass(merged_at))
    records, runs, capped = [], 0, False
    changed_passes = wanted_passes = 0
    system = system_prompt(repo, home.instructions())
    done: set[str] = set()
    while queue:
        t = queue.pop(0)
        if t in done:
            continue
        done.add(t)
        before = {r: store.log_length(r) for r in [number, *refs]}
        github.base = _feed(full, store, number, pulls, t, git, refs)
        github.head = store.header(number).head_sha
        arrived = [
            e for r in [number, *refs] for e in _records_after(store, r, before[r])
        ]
        if arrived or t == ceil_pass(opened):
            changed_passes += 1
            if t == ceil_pass(opened) or bool(news_for_agent(arrived, set())):
                wanted_passes += 1
        if runs >= MAX_RUNS_PER_PULL or spend.exhausted():
            capped = capped or runs >= MAX_RUNS_PER_PULL
            continue
        clock = lambda t=t: parse(t)  # noqa: E731
        if kind in ("codenib", "codenib-commits", "codenib-broad"):
            if home.book.get(number) is None:
                open_ticket(
                    home.book, number, directory=str(snapshot), checkout=snapshot
                )
            ticket = home.book.get(number)
            if not agent_mod._why(home, ticket, parse(t)):
                continue
            run = run_ticket(
                home, ticket, runner, checkout=snapshot, worker="replay",
                lease_seconds=runner.timeout + 300, client=github, now=clock,
                **({"review_new_commits": True} if kind == "codenib-commits" else {}),
            )  # fmt: skip
            record = dataclasses.asdict(run)
            log = home.runs()[-1] if home.runs() else {}
            record.update(
                {k: log.get(k) for k in ("agent_input_tokens", "agent_output_tokens",
                                          "turns", "models", "reason")}
            )  # fmt: skip
            if run.message:
                record["body"] = home.outbox.get(run.message).body
            record["notes"] = home.notes(number)
            record["follow_up"] = home.timers().get(str(number))
            record["shared_first_run"] = getattr(runner, "shared_at", "") == t
            if run.outcome != "skipped":
                runs += 1
                spend.add(run.cost_usd)
            timer = home.timers().get(str(number))
            if timer and timer["due"] <= merged_at:
                queue = sorted(set(queue) | {ceil_pass(timer["due"])})
        else:
            new = [
                e for r in [number, *refs] for e in _records_after(store, r, before[r])
            ]
            first = t == ceil_pass(opened)
            wanted = first or bool(news_for_agent(new, set()))
            if not new and not first:
                continue
            if kind == "stateless" and not wanted:
                continue
            bundle = load_bundle(store, number)
            packet = build_packet(bundle, checkout=snapshot)
            diff = write_pull_diff(github, home, number)
            prompt = run_prompt(
                number=number, why=["there is new activity"], notes="", said=[],
                own=[], thread=packet.text, diff=diff,
            )  # fmt: skip
            result = runner(
                system, prompt, snapshot, [home.dir / "diffs"] if diff else []
            )
            runs += 1
            spend.add(result.cost_usd)
            decision = result.decision or {}
            record = {
                "ticket": number,
                "outcome": decision.get("action", "failed"),
                "error": result.error,
                "prompt_tokens": count_tokens(system) + count_tokens(prompt),
                "thread_tokens": packet.tokens,
                "cost_usd": result.cost_usd,
                "seconds": round(result.seconds, 1),
                "agent_input_tokens": result.input_tokens,
                "agent_output_tokens": result.output_tokens,
                "turns": result.turns,
                "models": result.models,
                "reason": decision.get("reason", ""),
                # Would the codenib arm's filter have woken the agent for this?
                "news_calls_for_agent": wanted,
            }
            if decision.get("action") == "reply":
                record["body"] = decision["body"]
        record.update({"arm": arm, "number": number, "at": t, "head": github.head})
        records.append(record)
    result = {
        "number": number,
        "arm": arm,
        "opened": opened,
        "merged": merged_at,
        "passes": len(done),
        "passes_with_any_change": changed_passes,
        "passes_with_news_for_the_agent": wanted_passes,
        "runs": runs,
        "capped": capped,
        "budget_stop": spend.exhausted(),
        "records": records,
    }
    target = out / "results" / arm / f"{number}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=1))
    return result


class SharedFirstRun:
    """Answers the first run with another arm's first run on the same input."""

    def __init__(self, inner: ClaudeRunner, record: dict) -> None:
        self.inner = inner
        self.timeout = inner.timeout
        self.record = record
        self.shared_at = record["at"]

    def __call__(self, system, prompt, cwd, read_dirs=()):
        record, self.record = self.record, None
        if record is None:
            return self.inner(system, prompt, cwd, read_dirs)
        follow = record.get("follow_up") or {}
        hours = 0.0
        if follow.get("due"):
            hours = (parse(follow["due"]) - parse(record["at"])).total_seconds() / 3600
        action = {"reply": "reply", "ask": "ask"}.get(record["outcome"], "nothing")
        decision = agent_mod.check_decision(
            {
                "action": action,
                "body": record.get("body", ""),
                "notes": record.get("notes", ""),
                "follow_up_hours": hours,
                "follow_up_note": follow.get("note", ""),
                "reason": record.get("reason") or "",
            }
        )
        return agent_mod.RunResult(
            decision,
            "" if decision else "the shared first run gave no decision",
            record.get("seconds") or 0.0,
            record.get("cost_usd"),
            record.get("agent_input_tokens") or 0,
            record.get("agent_output_tokens") or 0,
            record.get("turns") or 0,
        )


def _records_after(store: ThreadStore, number: int, position: int) -> list:
    fresh = store.addresses_from(number, position)
    return [e for e in store.events(number) if e.address in fresh]


def replay(args: argparse.Namespace) -> int:
    out = Path(args.out)
    selection = json.loads((out / "selection.json").read_text())
    numbers = selection["pilot"] if args.pilot else selection["order"]
    if args.limit:
        numbers = numbers[: args.limit]
    spend = Spend(args.cap_usd)
    jobs = []
    for arm in args.arm:
        for number in numbers:
            if (out / "results" / arm / f"{number}.json").exists() and not args.redo:
                continue
            jobs.append((arm, number))
    print(f"{len(jobs)} replays", file=sys.stderr)

    def one(job):
        arm, number = job
        started = time.monotonic()
        try:
            result = replay_pull(out, arm, number, args, spend)
        except Exception as exc:  # noqa: BLE001 - keep the other replays going
            print(f"{arm} #{number}: failed: {exc!r}", file=sys.stderr)
            return
        print(
            f"{arm} #{number}: {result.get('runs', 0)} runs, "
            f"{time.monotonic() - started:.0f}s, spent ${spend.total:.2f}",
            file=sys.stderr,
        )

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(one, jobs))
    print(f"spent ${spend.total:.2f}", file=sys.stderr)
    return 0


# --------------------------------------------------------------------- judge

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "raised": {"type": "boolean"},
        "draft": {"type": "integer"},
        "why": {"type": "string"},
    },
    "required": ["raised", "draft", "why"],
    "additionalProperties": False,
}

JUDGE_PROMPT = """\
A maintainer reviewed a pull request and wrote the comment below. Before that, \
an assistant had drafted the replies listed after it.

Decide whether any draft raises the same concern as the maintainer, so that a \
reader of that draft would have learned the substance of the maintainer's \
point: the same bug, missing case, missing test, design question or requested \
change. Naming the same file or function is not enough. Praise, approval or a \
summary of the change does not count.

## The maintainer's comment
{where}
{hunk}
Comment:
{body}

## Earlier drafts
{drafts}

Answer with raised (true or false), draft (the number of the draft that \
raises it, 0 if none) and why (one sentence).
"""


def review_comments(full: ThreadStore, number: int) -> list:
    """What people other than the author wrote in review, after the first wake."""

    header = full.header(number)
    start = ceil_pass(header.created_at)
    return [
        e
        for e in full.events(number)
        if e.kind in ("review_comment", "review")
        and e.actor != header.author
        and is_human(e.actor, e.is_bot)
        and len(e.body.strip()) >= 30
        and e.at > start
    ]


def judge(args: argparse.Namespace) -> int:
    """Ask a model, blind to the arm, whether a draft raised a reviewer's point."""

    out = Path(args.out)
    selection = json.loads((out / "selection.json").read_text())
    full = ThreadStore(selection["repo"], out / "store")
    scratch = out / "judge" / "cwd"
    scratch.mkdir(parents=True, exist_ok=True)
    jobs = []
    for arm in args.arm:
        for path in sorted((out / "results" / arm).glob("*.json")):
            number = json.loads(path.read_text())["number"]
            target = out / "judge" / arm / f"{number}.json"
            if not target.exists():
                jobs.append((arm, number, target))
    lock = threading.Lock()
    spent = [0.0]

    def one(job):
        arm, number, target = job
        drafts = [
            r
            for r in json.loads((out / "results" / arm / f"{number}.json").read_text())[
                "records"
            ]
            if r.get("body")
        ]
        verdicts = []
        for comment in review_comments(full, number):
            earlier = [r for r in drafts if r["at"] < comment.at]
            verdict = {
                "comment": comment.address,
                "at": comment.at,
                "drafts": len(earlier),
            }
            if earlier:
                where = (
                    f"File: {comment.meta.get('path')}"
                    if comment.meta.get("path")
                    else "Review summary (no file)"
                )
                hunk = comment.meta.get("hunk") or ""
                hunk = f"Code it refers to:\n{hunk[-1500:]}\n" if hunk else ""
                listed = "\n\n".join(
                    f"### Draft {i}\n{r['body'][:4000]}"
                    for i, r in enumerate(earlier, 1)
                )
                prompt = JUDGE_PROMPT.format(
                    where=where, hunk=hunk, body=comment.body[:3000], drafts=listed
                )
                argv = [
                    "claude", "-p", "--model", args.model, "--output-format", "json",
                    "--json-schema", json.dumps(JUDGE_SCHEMA), "--tools", "",
                    "--strict-mcp-config", "--no-session-persistence", "--restricted",
                ]  # fmt: skip
                proc = subprocess.run(
                    argv, input=prompt, capture_output=True, text=True, cwd=scratch,
                    timeout=300,
                )  # fmt: skip
                try:
                    record = json.loads(proc.stdout)
                except ValueError:
                    record = {}
                found = record.get("structured_output") or {}
                verdict.update(
                    raised=bool(found.get("raised")),
                    draft=found.get("draft", 0),
                    why=found.get("why", ""),
                    error="" if found else (proc.stderr or proc.stdout)[-300:],
                )
                with lock:
                    spent[0] += float(record.get("total_cost_usd") or 0)
            else:
                verdict.update(raised=False, draft=0, why="no earlier draft")
            verdicts.append(verdict)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(verdicts, indent=1))
        print(f"{arm} #{number}: {sum(v['raised'] for v in verdicts)}/{len(verdicts)}, "
              f"judge ${spent[0]:.2f}", file=sys.stderr)  # fmt: skip

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(one, jobs))
    return 0


# --------------------------------------------------------------------- score


def _cited(body: str, path: str) -> bool:
    """Whether a draft names the file: its path, or its last three parts."""

    parts = path.split("/")
    tail = "/".join(parts[-3:]) if len(parts) > 3 else path
    return path in body or tail in body


def _bootstrap_ratio(pairs, rounds=5000, seed=7):
    rng = random.Random(seed)
    values = []
    for _ in range(rounds):
        sample = [pairs[rng.randrange(len(pairs))] for _ in pairs]
        b = sum(p[1] for p in sample)
        values.append(sum(p[0] for p in sample) / b if b else 0.0)
    values.sort()
    return values[int(0.025 * rounds)], values[int(0.975 * rounds)]


def score(args: argparse.Namespace) -> int:
    out = Path(args.out)
    selection = json.loads((out / "selection.json").read_text())
    pulls = json.loads((out / "pulls.json").read_text())
    full = ThreadStore(selection["repo"], out / "store")
    git = out / "git"
    results = {}
    for arm in ("codenib", "codenib-commits", "stateless", "naive"):
        for path in sorted((out / "results" / arm).glob("*.json")):
            data = json.loads(path.read_text())
            results.setdefault(arm, {})[data["number"]] = data
    other = args.against
    both = sorted(set(results.get("codenib", {})) & set(results.get(other, {})))
    if args.exclude_pilot:
        # The pilot calibrated the protocol, so it is reported apart.
        both = [n for n in both if n not in selection["pilot"]]
    elif args.only_pilot:
        both = [n for n in both if n in selection["pilot"]]
    summary = {
        "pulls": len(both),
        "paired_with": other,
        "instructions": INSTRUCTIONS,
        "arms": {},
    }

    truth = {}
    changed = {}
    for number in both:
        header = full.header(number)
        firsts = {}
        for e in full.events(number):
            if (
                e.kind == "review_comment"
                and e.actor != header.author
                and is_human(e.actor, e.is_bot)
            ):
                path = e.meta.get("path") or ""
                if path and (path not in firsts or e.at < firsts[path]):
                    firsts[path] = e.at
        # Only files first reviewed after the agent's first wake could have
        # been raised by it first.
        start = ceil_pass(header.created_at)
        truth[number] = {p: at for p, at in firsts.items() if at > start}
        info = pulls[str(number)]
        names = set()
        for sha, _ in info["commits"]:
            names |= {f["filename"] for f in diff_files(git, info["base"], sha)}
        changed[number] = names

    for arm in [
        a for a in ("codenib", "codenib-commits", "stateless", "naive") if a in results
    ]:
        numbers = [n for n in both if n in results[arm]]
        rows = [results[arm][n] for n in numbers]
        records = [r for row in rows for r in row["records"]]
        model = [r for r in records if r.get("outcome") != "skipped"]
        drafts = [r for r in model if r.get("outcome") == "reply"]
        cost = sum(r.get("cost_usd") or 0 for r in model)
        hit = cited = cited_good = repeats = 0
        for number in numbers:
            row_drafts = [r for r in results[arm][number]["records"] if r.get("body")]
            said: set[str] = set()
            for r in row_drafts:
                files = {p for p in changed[number] if _cited(r["body"], p)}
                if said and files <= said:
                    repeats += 1  # names no file an earlier draft had not
                said |= files
            for path, first in truth[number].items():
                if any(r["at"] < first and _cited(r["body"], path) for r in row_drafts):
                    hit += 1
            first_cite = {}
            for r in row_drafts:
                for path in changed[number]:
                    if _cited(r["body"], path) and path not in first_cite:
                        first_cite[path] = r["at"]
            cited += len(first_cite)
            cited_good += sum(
                1
                for path, at in first_cite.items()
                if path in truth[number] and truth[number][path] > at
            )
        summary["arms"][arm] = {
            "model_runs": len(model),
            "skipped_without_a_run": len(records) - len(model),
            "failed": sum(1 for r in model if r.get("outcome") == "failed"),
            "cost_usd": round(cost, 3),
            "cost_per_pull": round(cost / len(numbers), 4) if numbers else 0,
            "cost_per_run": round(cost / len(model), 4) if model else 0,
            "prompt_tokens_mean": (
                round(statistics.mean(r.get("prompt_tokens") or 0 for r in model))
                if model
                else 0
            ),
            "agent_input_tokens_mean": (
                round(statistics.mean(r.get("agent_input_tokens") or 0 for r in model))
                if model
                else 0
            ),
            "drafts": len(drafts),
            "drafts_per_pull": round(len(drafts) / len(numbers), 2) if numbers else 0,
            "repeat_drafts": repeats,
            "draft_rate": round(len(drafts) / len(model), 3) if model else 0,
            "reviewed_files": sum(len(truth[n]) for n in numbers),
            "reviewed_files_raised_first": hit,
            "files_cited": cited,
            "files_cited_then_reviewed": cited_good,
            "capped_pulls": sum(1 for row in rows if row["capped"]),
        }
        summary["arms"][arm]["pulls"] = len(numbers)
        summary["arms"][arm]["passes_with_any_change"] = sum(
            row["passes_with_any_change"] for row in rows
        )
        summary["arms"][arm]["passes_with_news_for_the_agent"] = sum(
            row["passes_with_news_for_the_agent"] for row in rows
        )
        if arm == "naive":
            summary["arms"][arm]["runs_the_filter_would_skip"] = sum(
                1 for r in model if not r.get("news_calls_for_agent", True)
            )
            summary["arms"][arm]["cost_of_those_runs"] = round(
                sum(
                    r.get("cost_usd") or 0
                    for r in model
                    if not r.get("news_calls_for_agent", True)
                ),
                3,
            )
    total_changed = sum(len(c) for c in changed.values())
    reviewed_changed = sum(len(set(truth[n]) & changed[n]) for n in both)
    summary["chance_precision"] = (
        round(reviewed_changed / total_changed, 3) if total_changed else 0
    )
    pairs = []
    for number in both:
        a = sum(r.get("cost_usd") or 0 for r in results["codenib"][number]["records"])
        b = sum(r.get("cost_usd") or 0 for r in results[other][number]["records"])
        pairs.append((a, b))
    if pairs and sum(p[1] for p in pairs):
        summary["cost_ratio"] = round(
            sum(p[0] for p in pairs) / sum(p[1] for p in pairs), 3
        )
        summary["cost_ratio_ci95"] = [round(x, 3) for x in _bootstrap_ratio(pairs)]
    # The same wake in both arms: what reading only the news saves per run.
    same = []
    for number in both:
        a = {
            r["at"]: r
            for r in results["codenib"][number]["records"]
            if r.get("outcome") != "skipped"
        }
        b = {
            r["at"]: r
            for r in results[other][number]["records"]
            if r.get("outcome") != "skipped"
        }
        for at in sorted(set(a) & set(b)):
            same.append((number, a[at], b[at]))
    later = [s for s in same if s[1]["at"] != ceil_pass(full.header(s[0]).created_at)]
    if later:
        summary["same_wakes_after_the_first"] = {
            "wakes": len(later),
            "codenib_cost": round(sum(s[1].get("cost_usd") or 0 for s in later), 3),
            "other_cost": round(sum(s[2].get("cost_usd") or 0 for s in later), 3),
            "codenib_prompt_tokens_mean": round(
                statistics.mean(s[1].get("prompt_tokens") or 0 for s in later)
            ),
            "other_prompt_tokens_mean": round(
                statistics.mean(s[2].get("prompt_tokens") or 0 for s in later)
            ),
            "codenib_agent_input_tokens_mean": round(
                statistics.mean(s[1].get("agent_input_tokens") or 0 for s in later)
            ),
            "other_agent_input_tokens_mean": round(
                statistics.mean(s[2].get("agent_input_tokens") or 0 for s in later)
            ),
            "codenib_drafts": sum(1 for s in later if s[1].get("outcome") == "reply"),
            "other_drafts": sum(1 for s in later if s[2].get("outcome") == "reply"),
        }
        by_pull = {}
        for number, a, b in later:
            x = by_pull.setdefault(number, [0.0, 0.0])
            x[0] += a.get("cost_usd") or 0
            x[1] += b.get("cost_usd") or 0
        pairs_later = list(by_pull.values())
        summary["same_wakes_after_the_first"]["cost_ratio_ci95"] = [
            round(x, 3) for x in _bootstrap_ratio(pairs_later)
        ]
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    steps = parser.add_subparsers(dest="step", required=True)
    s = steps.add_parser("select")
    s.add_argument("--out", required=True)
    s.add_argument("--repo", default=REPO)
    s.add_argument("--merged", default="2026-10-01..2026-10-31")
    s.add_argument("--n", type=int, default=50)
    s.add_argument("--seed", type=int, default=SEED)
    s.add_argument(
        "--max-eligible", type=int, help="stop checking candidates after this many"
    )
    s.set_defaults(handler=select)
    rf = steps.add_parser("refine")
    rf.add_argument("--out", required=True)
    rf.add_argument("--n", type=int, default=50)
    rf.set_defaults(handler=refine)
    f = steps.add_parser("fetch")
    f.add_argument("--out", required=True)
    f.add_argument("--reference", help="a local clone to share objects with")
    f.add_argument("--limit", type=int)
    f.set_defaults(handler=fetch)
    r = steps.add_parser("replay")
    r.add_argument("--out", required=True)
    r.add_argument(
        "--arm",
        nargs="+",
        help="codenib, codenib-commits, codenib-broad, stateless or naive; "
        "add .r2, .r3 for a repeat",
        default=["codenib", "stateless"],
    )
    r.add_argument("--pilot", action="store_true", help="only the first pull requests")
    r.add_argument("--limit", type=int)
    r.add_argument("--workers", type=int, default=4)
    r.add_argument(
        "--cap-usd", type=float, default=40.0, help="stop starting runs after this"
    )
    r.add_argument("--model", default="sonnet")
    r.add_argument("--max-budget-usd", type=float, default=0.5)
    r.add_argument("--timeout", type=int, default=10, help="minutes per run")
    r.add_argument("--redo", action="store_true")
    r.set_defaults(handler=replay)
    j = steps.add_parser("judge")
    j.add_argument("--out", required=True)
    j.add_argument(
        "--arm", nargs="+", default=["codenib", "codenib-commits", "stateless", "naive"]
    )
    j.add_argument("--model", default="sonnet")
    j.add_argument("--workers", type=int, default=8)
    j.set_defaults(handler=judge)
    c = steps.add_parser("score")
    c.add_argument("--out", required=True)
    c.add_argument("--exclude-pilot", action="store_true")
    c.add_argument(
        "--against", default="stateless", help="the arm codenib is paired with"
    )
    c.add_argument("--only-pilot", action="store_true")
    c.set_defaults(handler=score)
    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
