#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Benchmark thread context serving against an agent that reads threads itself.

A task is a closed issue as it stood just before the pull request that fixed
it was opened. The agent gets a one-commit snapshot of the repository at that
point and must say which files and symbols have to change. The answer is
scored against what the merged pull request changed.

Arms differ only in how thread context reaches the agent:

* ``gh``      nothing is given; the agent reads threads with a ``gh`` replica.
* ``dump``    every thread event is put in the prompt, unprocessed.
* ``codenib`` the ``codenib threads`` packet is put in the prompt.

The ``gh`` replica is available in every arm. It serves only events that
existed at the cut-off, so no arm can read the fix.

    benchmark_threads_context.py tasks --store S --clones C --out O REPO...
    benchmark_threads_context.py run   --out O [--arms ...] [--limit N]
    benchmark_threads_context.py score --out O
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from codenib.threads.events import ThreadEvent
from codenib.threads.log_store import ThreadStore
from codenib.threads.serve import (
    Bundle,
    ThreadView,
    build_packet,
    count_tokens,
    load_bundle,
    render_raw,
)
from codenib.threads.touched import language_for_path, merge_base, touched_between

ARMS = ("gh", "dump", "codenib")
NON_SOURCE_RE = re.compile(
    r"(^|/)(tests?|testdata|fixtures?|snapshots?|resources|docs?|examples?|"
    r"benches|scripts)(/|$)|_test\.go$|(^|/)test_[^/]*\.py$|\.snap$|\.md$"
)
DUMP_TOKEN_CAP = 60_000

OPENING = {
    "issue": "You are taking over issue #{number} of the GitHub repository {repo}.",
    "pr": (
        "You are taking over pull request #{number} of the GitHub repository "
        "{repo} from its author. It is not merged yet."
    ),
}
SNAPSHOT = {
    "issue": "at the commit you would start from",
    "pr": "at the current head of the pull request branch",
}
QUESTION = {
    "issue": (
        "Work out what has to change in the code to resolve the issue, taking into\n"
        "account what was already discussed, tried and decided in the issue and in any\n"
        "related issue or pull request."
    ),
    "pr": (
        "Work out what has to change next in the code so that the pull request can be\n"
        "merged: what reviewers asked for and what failing checks require, taking into\n"
        "account the issue it addresses. Changes the branch already contains do not count."
    ),
}

PROMPT = """\
{opening}

The working directory is a snapshot of the repository {snapshot}. It has no
history. Do not modify any file.

There is no network. `{gh}` is a read-only replica of the
repository's issues and pull requests as of now. Call it by that full path. It
supports:
  gh issue view N [--comments]      gh pr view N [--comments]
  gh pr checks N                    gh pr diff N --name-only
  gh run view CHECK_ID --log-failed
{context}
{question} Read as much or as little as you need.

Finish with one JSON object and nothing after it:
{{"files": ["path", ...], "symbols": ["name", ...], "plan": "..."}}
- files: the source files that must change, most important first, at most 5.
  Do not list tests, fixtures, snapshots or docs.
- symbols: the functions, methods or types that must change, at most 8.
- plan: three sentences at most, including any decision from the threads that
  constrains the change.
"""

CONTEXT_NOTE = """
The thread context below was prepared for you. It may be all you need from the
threads; use the replica only for what it does not cover.

<thread_context>
{body}
</thread_context>
"""

GH_REPLICA = r'''#!/usr/bin/env python3
"""Read-only replica of gh for one benchmark task (events up to the cut-off)."""
import json, sys
from pathlib import Path

DATA = json.loads((Path(__file__).resolve().parent / "threads.json").read_text())
FAILED = {"failure", "timed_out", "startup_failure"}


def thread(number):
    t = DATA["threads"].get(str(number))
    if t is None:
        sys.exit(
            "GraphQL: Could not resolve to an issue or pull request with the "
            f"number of {number}."
        )
    return t


def view(number, comments):
    t = thread(number)
    opened = next((e for e in t["events"] if e["kind"] == "opened"), None)
    print(f"title:\t{t['title']}\nstate:\t{t['state'].upper()}\nauthor:\t{t['author']}")
    print(f"labels:\t{', '.join(t['labels'])}\nnumber:\t{number}\n--")
    print(opened["body"] if opened else "")
    refs = [e for e in t["events"] if e["kind"] == "cross_reference"]
    for e in refs:
        m = e["meta"]
        kind = "pr" if m.get("is_pr") else "issue"
        print(f"-- referenced by #{m.get('from')} ({kind}): {m.get('title')}")
    if not comments:
        n = sum(1 for e in t["events"] if e["kind"] in ("comment", "review", "review_comment"))
        print(f"--\n{n} comments and reviews not shown; pass --comments")
        return
    for e in t["events"]:
        if e["kind"] in ("comment", "review", "review_comment"):
            m = e["meta"]
            where = f" on {m.get('path')}:{m.get('line')}" if m.get("path") else ""
            state = f" [{m.get('state')}]" if m.get("state") else ""
            print(f"--\nauthor:\t{e['actor']}{state}{where}\ndate:\t{e['at']}\n")
            if m.get("hunk"):
                print(m["hunk"])
            print(e["body"])


def main(argv):
    if len(argv) >= 3 and argv[0] in ("issue", "pr") and argv[1] == "view":
        return view(argv[2].lstrip("#"), "--comments" in argv or "-c" in argv)
    if len(argv) >= 3 and argv[:2] == ["pr", "checks"]:
        t = thread(argv[2].lstrip("#"))
        checks = [e for e in t["events"] if e["kind"] == "check"]
        if not checks:
            print("no checks reported")
        for e in checks:
            m = e["meta"]
            ident = e["address"].rsplit("/", 1)[-1]
            print(f"{m.get('name')}\t{m.get('conclusion')}\t{str(m.get('sha'))[:8]}\t{ident}")
        return
    if len(argv) >= 3 and argv[:2] == ["pr", "diff"]:
        t = thread(argv[2].lstrip("#"))
        diffs = [e for e in t["events"] if e["kind"] == "diff"]
        if not diffs:
            print("diff not available")
        for path in (diffs[-1]["meta"].get("files", []) if diffs else []):
            print(path)
        return
    if len(argv) >= 3 and argv[:2] == ["run", "view"]:
        for t in DATA["threads"].values():
            for e in t["events"]:
                if e["kind"] == "check" and e["address"].endswith("/" + argv[2]):
                    print(e["meta"].get("log") or "log not available")
                    return
        sys.exit("run not found")
    sys.exit("gh replica: supported commands are issue view, pr view, pr checks, "
             "pr diff --name-only, run view ID --log-failed")


main(sys.argv[1:])
'''


def sh(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}: {proc.stderr.strip()[:300]}")
    return proc.stdout


def is_source(path: str) -> bool:
    return language_for_path(path) is not None and not NON_SOURCE_RE.search(path)


def before(iso: str) -> str:
    moment = datetime.fromisoformat(iso.replace("Z", "+00:00")) - timedelta(seconds=1)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ tasks ---


def fixing_pull(store: ThreadStore, number: int):
    """The earliest merged pull request that closes the issue."""

    header = store.header(number)
    pulls = []
    for ref in header.links:
        other = store.header(int(ref)) if ref.isdigit() else None
        if not other or not other.is_pr:
            continue
        events = store.events(other.number)
        merged = any(e.kind == "merged" for e in events)
        if merged and other.links.get(str(number)) == "closes":
            pulls.append(other)
    return min(pulls, key=lambda p: p.created_at) if pulls else None


def replica_data(bundle: Bundle) -> dict:
    threads = {}
    for view in bundle.views:
        threads[str(view.number)] = {
            "title": view.header.title,
            "state": view.state(),
            "author": view.header.author,
            "labels": view.labels(),
            "events": [e.to_json() for e in view.events],
        }
    return {"threads": threads}


def dump_text(bundle: Bundle) -> str:
    """Every event, unprocessed, with failed-job logs cut to their last 200 lines.

    The tail of a log is what a simple harness would keep. Whatever is still
    above the cap is cut from the end.
    """

    def tail(view):
        events = []
        for event in view.events:
            log = event.meta.get("log")
            if log and log.count("\n") > 200:
                meta = {**event.meta, "log": "\n".join(log.splitlines()[-200:])}
                event = ThreadEvent.from_json({**event.to_json(), "meta": meta})
            events.append(event)
        return ThreadView(view.header, events, view.relation)

    text = render_raw(Bundle(tail(bundle.root), [tail(v) for v in bundle.linked]))
    tokens = count_tokens(text)
    if tokens > DUMP_TOKEN_CAP:
        keep = int(len(text) * DUMP_TOKEN_CAP / tokens)
        text = text[:keep] + "\n[... truncated at the 60k token cap ...]\n"
    return text


def materialize(
    out: Path,
    task: dict,
    clone: Path,
    bundle: Bundle,
    budget: int,
    code_budget: int = 1500,
) -> dict:
    """Write the snapshot, the gh replica and both prepared contexts of a task."""

    task_dir = out / "tasks" / task["id"]
    snapshot = task_dir / "repo"
    if not snapshot.exists():
        snapshot.mkdir(parents=True)
        archive = subprocess.Popen(
            ["git", "-C", str(clone), "archive", task["base"]], stdout=subprocess.PIPE
        )
        subprocess.run(
            ["tar", "-x", "-C", str(snapshot)], stdin=archive.stdout, check=True
        )
        archive.wait()
        sh("git", "init", "-q", cwd=snapshot)
        sh("git", "add", "-A", cwd=snapshot)
        sh(
            "git", "-c", "user.name=bench", "-c", "user.email=b@b",
            "commit", "-qm", "snapshot", cwd=snapshot,
        )  # fmt: skip
    bin_dir = task_dir / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "threads.json").write_text(json.dumps(replica_data(bundle)))
    (bin_dir / "gh").write_text(GH_REPLICA)
    (bin_dir / "gh").chmod(0o755)
    started = time.time()
    packet = build_packet(
        bundle,
        budget=budget,
        code_budget=code_budget,
        checkout=clone,
        rev=task["base"],
    )
    task["serve_seconds"] = round(time.time() - started, 2)
    dump = dump_text(bundle)
    (task_dir / "context.codenib.md").write_text(packet.text)
    (task_dir / "context.dump.md").write_text(dump)
    task["threads"] = [v.number for v in bundle.views]
    task["events"] = sum(len(v.events) for v in bundle.views)
    task["raw_tokens"] = count_tokens(render_raw(bundle))
    task["context_tokens"] = {"codenib": packet.tokens, "dump": count_tokens(dump)}
    task["anchor_files"] = sorted({a.path for a in packet.anchors})
    (task_dir / "task.json").write_text(json.dumps(task, indent=2))
    print(
        f"{task['id']}: threads={len(task['threads'])} events={task['events']} "
        f"gt_files={len(task['gt_files'])} raw={task['raw_tokens']} "
        f"packet={packet.tokens} dump={task['context_tokens']['dump']}",
        file=sys.stderr,
    )
    return task


def pr_task(store: ThreadStore, clone: Path, number: int, max_files: int):
    """A merged pull request cut just before its first commit after feedback.

    The answer is what the next round of commits changed: from the head at the
    cut to the last commit before further feedback arrived.
    """

    header = store.header(number)
    events = store.events(number)
    if not header or not header.is_pr or not any(e.kind == "merged" for e in events):
        return None

    def feedback(event) -> bool:
        if event.kind == "check":
            return event.meta.get("conclusion") in ("failure", "timed_out")
        return (
            event.kind in ("review", "review_comment", "comment")
            and not event.is_bot
            and event.actor != header.author
            and bool(event.body.strip())
        )

    commits = [e for e in events if e.kind == "commit"]
    given = [e for e in events if feedback(e)]
    if len(commits) < 2 or not given:
        return None
    later = [c for c in commits if c.at > given[0].at]
    earlier = [c for c in commits if c.at <= given[0].at]
    if not later or not earlier:
        return None
    cut = later[0].at
    further = next((e.at for e in given if e.at > cut), "9999")
    round_commits = [c for c in later if c.at < further] or later[:1]
    base, target = earlier[-1].meta["sha"], round_commits[-1].meta["sha"]
    touched = touched_between(clone, base, target)
    if touched is None:
        return None
    gt_files = sorted(p for p in touched.files if is_source(p))
    if not 1 <= len(gt_files) <= max_files:
        return None
    return {
        "id": f"{store.repo.replace('/', '__')}-pr{number}",
        "kind": "pr",
        "repo": store.repo,
        "number": number,
        "cutoff": before(cut),
        "base": base,
        "target": target,
        "gt_files": gt_files,
        "gt_symbols": sorted(
            s for s in touched.symbols if is_source(s.split(":", 1)[0])
        ),
    }


def build_pr_tasks(args: argparse.Namespace) -> int:
    out = Path(args.out)
    tasks = []
    for repo in args.repos:
        store = ThreadStore(repo, Path(args.store))
        clone = Path(args.clones) / (repo.split("/")[1] + ".git")
        for number in store.numbers():
            task = pr_task(store, clone, number, args.max_files)
            if task is None:
                continue
            bundle = load_bundle(store, number, at=task["cutoff"])
            if bundle is None:
                continue
            tasks.append(
                materialize(out, task, clone, bundle, args.budget, args.code_budget)
            )
    (out / "tasks.json").write_text(json.dumps(tasks, indent=2))
    print(f"{len(tasks)} tasks", file=sys.stderr)
    return 0


def build_tasks(args: argparse.Namespace) -> int:
    if args.scenario == "pr":
        return build_pr_tasks(args)
    out = Path(args.out)
    tasks = []
    for repo in args.repos:
        store = ThreadStore(repo, Path(args.store))
        clone = Path(args.clones) / (repo.split("/")[1] + ".git")
        default = sh("git", "-C", str(clone), "symbolic-ref", "HEAD").strip()
        for number in store.numbers():
            header = store.header(number)
            if header is None or header.is_pr:
                continue
            pull = fixing_pull(store, number)
            if pull is None:
                continue
            head = f"refs/pull/{pull.number}/head"
            base = merge_base(clone, default, head)
            touched = touched_between(clone, base, head) if base else None
            if touched is None:
                continue
            gt_files = sorted(p for p in touched.files if is_source(p))
            if not 1 <= len(gt_files) <= args.max_files:
                continue
            gt_symbols = sorted(
                s for s in touched.symbols if is_source(s.split(":", 1)[0])
            )
            cutoff = before(pull.created_at)
            bundle = load_bundle(store, number, at=cutoff)
            if bundle is None:
                continue
            task = {
                "id": f"{repo.replace('/', '__')}-{number}",
                "kind": "issue",
                "repo": repo,
                "number": number,
                "fix_pr": pull.number,
                "cutoff": cutoff,
                "base": base,
                "gt_files": gt_files,
                "gt_symbols": gt_symbols,
            }
            tasks.append(
                materialize(out, task, clone, bundle, args.budget, args.code_budget)
            )
    (out / "tasks.json").write_text(json.dumps(tasks, indent=2))
    print(f"{len(tasks)} tasks", file=sys.stderr)
    return 0


# -------------------------------------------------------------------- run ---


def parse_answer(text: str) -> dict:
    for match in reversed(list(re.finditer(r"\{", text))):
        try:
            value = json.loads(text[match.start() :].strip().rstrip("`").strip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "files" in value:
            return value
    return {}


_GH_CALL_RE = re.compile(r"(^|[\s'\"/;&|])gh\s")


def codex_command(task_dir: Path, env: dict, args: argparse.Namespace) -> list[str]:
    command = [
        "codex", "exec", "--json", "--sandbox", "read-only",
        "-c", f"model_reasoning_effort={args.effort}",
        "-c", f"shell_environment_policy.set.PATH={env['PATH']}",
        "-C", str(task_dir / "repo"),
    ]  # fmt: skip
    return command + (["-m", args.model] if args.model else [])


def codex_parse(stdout: str):
    usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    commands, gh_calls, gh_chars, last_message = [], 0, 0, ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "turn.completed":
            for key in usage:
                usage[key] += event.get("usage", {}).get(key, 0)
        item = event.get("item") or {}
        if event.get("type") == "item.completed":
            if item.get("type") == "command_execution":
                commands.append(item.get("command", ""))
                if _GH_CALL_RE.search(item.get("command", "")):
                    gh_calls += 1
                    gh_chars += len(item.get("aggregated_output", ""))
            elif item.get("type") == "agent_message":
                last_message = item.get("text", "")
    return usage, commands, gh_calls, gh_chars, last_message, None


# Claude Code runs Bash inside its sandbox with no allowed network domain, and
# refuses to start if the sandbox cannot be set up. A command that needs the
# network is denied, so the agent cannot reach GitHub and read the fix.
CLAUDE_SETTINGS = {
    "sandbox": {
        "enabled": True,
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        "autoAllowBashIfSandboxed": True,
        "excludedCommands": [],
        "network": {"allowedDomains": []},
    }
}


def claude_command(task_dir: Path, env: dict, args: argparse.Namespace) -> list[str]:
    return [
        "claude", "-p", "--bare", "--output-format", "stream-json", "--verbose",
        "--model", args.model or "claude-opus-5-5", "--effort", args.effort,
        "--tools", "Bash,Read,Grep,Glob", "--permission-mode", "dontAsk",
        "--strict-mcp-config", "--no-session-persistence",
        "--max-budget-usd", str(args.max_budget_usd),
        "--settings", json.dumps(CLAUDE_SETTINGS),
    ]  # fmt: skip


def claude_parse(stdout: str):
    """Usage in the same shape as Codex: all input tokens, and the cached part."""

    usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    calls: dict[str, str] = {}
    commands, gh_calls, gh_chars, last_message, cost = [], 0, 0, "", None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = event.get("message")
        content = (message.get("content") or []) if isinstance(message, dict) else []
        if isinstance(content, str):
            content = []
        if event.get("type") == "assistant":
            for block in content:
                if block.get("type") == "tool_use":
                    spec = block.get("input") or {}
                    text = spec.get("command") or json.dumps(spec)
                    commands.append(text)
                    calls[block.get("id", "")] = text
                elif block.get("type") == "text":
                    last_message = block.get("text", "")
        elif event.get("type") == "user":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    if _GH_CALL_RE.search(calls.get(block.get("tool_use_id", ""), "")):
                        gh_calls += 1
                        gh_chars += len(str(block.get("content", "")))
        elif event.get("type") == "result":
            spent = event.get("usage") or {}
            cached = spent.get("cache_read_input_tokens", 0)
            usage = {
                "input_tokens": spent.get("input_tokens", 0)
                + spent.get("cache_creation_input_tokens", 0)
                + cached,
                "cached_input_tokens": cached,
                "output_tokens": spent.get("output_tokens", 0),
            }
            cost = event.get("total_cost_usd")
            last_message = event.get("result") or last_message
    return usage, commands, gh_calls, gh_chars, last_message, cost


AGENTS = {
    "codex": {"command": codex_command, "parse": codex_parse},
    "claude": {"command": claude_command, "parse": claude_parse},
}


def run_one(
    out: Path, task: dict, arm: str, rep: int, args: argparse.Namespace
) -> dict:
    task_dir = out / "tasks" / task["id"]
    stem = f"{task['id']}.{arm}" + (f".r{rep}" if rep else "")
    result_path = out / "runs" / f"{stem}.json"
    if result_path.exists() and not args.force:
        return json.loads(result_path.read_text())
    context = ""
    if arm != "gh":
        body = (task_dir / f"context.{arm}.md").read_text()
        context = CONTEXT_NOTE.format(body=body)
    kind = task.get("kind", "issue")
    prompt = PROMPT.format(
        opening=OPENING[kind].format(number=task["number"], repo=task["repo"]),
        snapshot=SNAPSHOT[kind],
        question=QUESTION[kind],
        context=context,
        gh=task_dir / "bin" / "gh",
    )
    env = dict(os.environ)
    env["PATH"] = f"{task_dir / 'bin'}:{env['PATH']}"
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        env.pop(name, None)
    command = AGENTS[args.agent]["command"](task_dir, env, args)
    started = time.time()
    try:
        proc = subprocess.run(
            command, input=prompt, capture_output=True, text=True,
            env=env, timeout=args.timeout, cwd=task_dir / "repo",
        )  # fmt: skip
        stdout, timed_out = proc.stdout, False
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or b"").decode("utf-8", "replace")
        timed_out = True
    usage, commands, gh_calls, gh_chars, last_message, cost = AGENTS[args.agent][
        "parse"
    ](stdout)
    result = {
        "task": task["id"],
        "arm": arm,
        "seconds": round(time.time() - started, 1),
        "timed_out": timed_out,
        "agent": args.agent,
        "usage": usage,
        "cost_usd": cost,
        "commands": len(commands),
        "gh_calls": gh_calls,
        "gh_output_tokens_est": gh_chars // 4,
        "answer": parse_answer(last_message),
        "last_message": last_message[-3000:],
    }
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2))
    (out / "runs" / f"{stem}.jsonl").write_text(stdout)
    return result


def run(args: argparse.Namespace) -> int:
    out = Path(args.out)
    tasks = json.loads((out / "tasks.json").read_text())
    if args.only:
        tasks = [t for t in tasks if t["id"] in set(args.only)]
    tasks = tasks[: args.limit] if args.limit else tasks
    jobs = [
        (task, arm, rep)
        for rep in range(args.reps)
        for task in tasks
        for arm in args.arms
    ]
    with ThreadPoolExecutor(args.workers) as pool:
        for result in pool.map(lambda job: run_one(out, *job, args), jobs):
            answer = result["answer"]
            print(
                f"{result['task']} {result['arm']}: {result['seconds']}s "
                f"in={result['usage']['input_tokens']} cmds={result['commands']} "
                f"files={len(answer.get('files', []))}",
                file=sys.stderr,
            )
    return 0


# ------------------------------------------------------------------ score ---


def _bare(symbol: str) -> str:
    symbol = symbol.split(":", 1)[-1] if "/" in symbol.split(":", 1)[0] else symbol
    symbol = re.sub(r"\(.*$", "", symbol.strip().strip("`"))
    return re.split(r"::|\.|\s", symbol)[-1].lower()


def score_run(task: dict, result: dict) -> dict:
    answer = result.get("answer") or {}
    files = [str(f).lstrip("./") for f in answer.get("files", [])][:5]
    gt = set(task["gt_files"])
    hit = [f for f in files if f in gt]
    gt_symbols = {_bare(s) for s in task["gt_symbols"]} - {"impl", ""}
    symbols = {_bare(str(s)) for s in answer.get("symbols", [])[:8]}
    usage = result["usage"]
    return {
        "answered": bool(files),
        "file_recall": len(hit) / len(gt),
        "file_precision": len(hit) / len(files) if files else 0.0,
        "file_hit": bool(hit),
        "top1": bool(files) and files[0] in gt,
        "symbol_recall": (
            len(symbols & gt_symbols) / len(gt_symbols) if gt_symbols else None
        ),
        "approach": (result.get("judge") or {}).get("approach"),
        "approach_same": (
            None
            if (result.get("judge") or {}).get("approach") is None
            else (result["judge"]["approach"] == 2)
        ),
        "cost_usd": result.get("cost_usd"),
        "input_tokens": usage["input_tokens"],
        "uncached_input": usage["input_tokens"] - usage["cached_input_tokens"],
        "output_tokens": usage["output_tokens"],
        "commands": result["commands"],
        "gh_calls": result["gh_calls"],
        "seconds": result["seconds"],
    }


NUMERIC = (
    "file_recall", "file_precision", "file_hit", "top1", "symbol_recall",
    "approach_same", "cost_usd", "input_tokens", "uncached_input",
    "output_tokens", "commands", "gh_calls", "seconds",
)  # fmt: skip
STRATA = (
    ("under 5k", 0, 5_000),
    ("5k to 20k", 5_000, 20_000),
    ("over 20k", 20_000, 10**12),
)


def _mean(values: list) -> float | None:
    values = [float(v) for v in values if v is not None]
    return statistics.mean(values) if values else None


def _bootstrap(pairs: list[tuple[float, float]], stat, rounds: int = 5000):
    """95% interval of ``stat`` over tasks resampled with replacement."""

    import random

    rng = random.Random(0)
    draws = sorted(
        stat([pairs[rng.randrange(len(pairs))] for _ in pairs]) for _ in range(rounds)
    )
    return draws[int(0.025 * rounds)], draws[int(0.975 * rounds)]


def _ratio(pairs) -> float:
    base = sum(b for _, b in pairs)
    return sum(a for a, _ in pairs) / base - 1 if base else 0.0


def _diff(pairs) -> float:
    return statistics.mean(a - b for a, b in pairs)


def load_scores(out: Path) -> tuple[dict, dict]:
    """Per task and arm, each metric averaged over that arm's repetitions."""

    tasks = {t["id"]: t for t in json.loads((out / "tasks.json").read_text())}
    runs: dict[str, dict[str, list[dict]]] = {}
    for path in sorted((out / "runs").glob("*.json")):
        result = json.loads(path.read_text())
        if result["task"] in tasks:
            runs.setdefault(result["task"], {}).setdefault(result["arm"], []).append(
                score_run(tasks[result["task"]], result)
            )
    rows = {
        task: {
            arm: {
                **{name: _mean([r[name] for r in reps]) for name in NUMERIC},
                "reps": len(reps),
            }
            for arm, reps in arms.items()
        }
        for task, arms in runs.items()
    }
    return tasks, rows


def report(tasks: dict, rows: dict, arms: list[str], title: str) -> None:
    print(f"\n### {title}: {len(rows)} tasks\n")
    if not rows:
        return
    print("| metric | " + " | ".join(arms) + " |")
    print("|---|" + "---|" * len(arms))
    table = (
        ("file_recall", "files found (recall)", "mean", "{:.2f}"),
        ("top1", "first file correct", "mean", "{:.2f}"),
        ("symbol_recall", "symbols found (recall)", "mean", "{:.2f}"),
        ("approach_same", "plan matches merged fix", "mean", "{:.2f}"),
        ("input_tokens", "input tokens per task", "median", "{:,.0f}"),
        ("input_tokens", "input tokens, all tasks", "sum", "{:,.0f}"),
        ("uncached_input", "uncached input, all tasks", "sum", "{:,.0f}"),
        ("output_tokens", "output tokens, all tasks", "sum", "{:,.0f}"),
        ("cost_usd", "reported cost, all tasks (USD)", "sum", "{:.2f}"),
        ("commands", "commands per task", "median", "{:.1f}"),
        ("gh_calls", "thread reads per task", "mean", "{:.1f}"),
        ("seconds", "seconds per task", "median", "{:.0f}"),
    )
    how = {"mean": statistics.mean, "median": statistics.median, "sum": sum}
    for name, label, agg, fmt in table:
        cells = []
        for arm in arms:
            values = [r[arm][name] for r in rows.values() if r[arm][name] is not None]
            cells.append(fmt.format(how[agg](values)) if values else "-")
        print(f"| {label} | " + " | ".join(cells) + " |")
    if "codenib" not in arms or len(rows) < 5:
        return
    print("\ncodenib against each baseline, paired by task, 95% bootstrap interval:\n")
    print("| against | input tokens | uncached input | files found | commands |")
    print("|---|---|---|---|---|")
    for base in arms:
        if base == "codenib":
            continue
        cells = []
        for name, stat, fmt in (
            ("input_tokens", _ratio, "{:+.0%}"),
            ("uncached_input", _ratio, "{:+.0%}"),
            ("file_recall", _diff, "{:+.2f}"),
            ("commands", _ratio, "{:+.0%}"),
        ):
            pairs = [(r["codenib"][name], r[base][name]) for r in rows.values()]
            low, high = _bootstrap(pairs, stat)
            cells.append(
                f"{fmt.format(stat(pairs))} ({fmt.format(low)} to {fmt.format(high)})"
            )
        print(f"| {base} | " + " | ".join(cells) + " |")


def score(args: argparse.Namespace) -> int:
    merged_tasks: dict = {}
    merged_rows: dict = {}
    for out in map(Path, args.out):
        tasks, rows = load_scores(out)
        merged_tasks.update(tasks)
        merged_rows.update(rows)
        (out / "scores.json").write_text(json.dumps(rows, indent=2))
    arms = [a for a in ARMS if any(a in r for r in merged_rows.values())]
    rows = {t: r for t, r in merged_rows.items() if all(a in r for a in arms)}
    report(merged_tasks, rows, arms, "All tasks")
    for label, low, high in STRATA:
        subset = {
            t: r
            for t, r in rows.items()
            if low <= merged_tasks[t].get("raw_tokens", 0) < high
        }
        report(merged_tasks, subset, arms, f"Raw thread context {label} tokens")
    return 0


# ------------------------------------------------------------------ judge ---

JUDGE_PROMPT = """\
You are grading a proposed plan for fixing a GitHub issue against the fix that
maintainers actually merged. Judge the approach, not the wording. Do not run
any command.

Merged fix (reference):
title: {title}
files changed: {files}
symbols changed: {symbols}
description:
{body}

Proposed plan:
files: {answer_files}
symbols: {answer_symbols}
plan: {plan}

Reply with one JSON object and nothing else:
{{"approach": 0, 1 or 2, "reason": "one sentence"}}
2 = the same approach as the merged fix.
1 = a related approach that addresses the issue but differs in a way that
    matters (different behaviour, or a direction the reference rules out).
0 = wrong, missing, or does not address what the merged fix addresses.
"""


def judge_one(out: Path, store_root: Path, task: dict, path: Path, args) -> None:
    result = json.loads(path.read_text())
    if "judge" in result and not args.force:
        return
    answer = result.get("answer") or {}
    if not answer.get("plan"):
        result["judge"] = {"approach": 0, "reason": "no answer"}
    else:
        store = ThreadStore(task["repo"], store_root)
        opened = next(e for e in store.events(task["fix_pr"]) if e.kind == "opened")
        prompt = JUDGE_PROMPT.format(
            title=store.header(task["fix_pr"]).title,
            files=", ".join(task["gt_files"]),
            symbols=", ".join(s.split(":", 1)[-1] for s in task["gt_symbols"][:20]),
            body=opened.body[:5000],
            answer_files=", ".join(map(str, answer.get("files", []))),
            answer_symbols=", ".join(map(str, answer.get("symbols", []))),
            plan=str(answer.get("plan"))[:3000],
        )
        proc = subprocess.run(
            ["codex", "exec", "--json", "--sandbox", "read-only",
             "--skip-git-repo-check", "-c", "model_reasoning_effort=low",
             "-C", str(out)],
            input=prompt, capture_output=True, text=True, timeout=600,
        )  # fmt: skip
        verdict = {}
        for line in proc.stdout.splitlines():
            try:
                item = json.loads(line).get("item") or {}
            except json.JSONDecodeError:
                continue
            if item.get("type") == "agent_message":
                match = re.search(r"\{.*\}", item.get("text", ""), re.S)
                try:
                    verdict = json.loads(match.group(0)) if match else verdict
                except json.JSONDecodeError:
                    pass
        result["judge"] = verdict or {"approach": None, "reason": "judge failed"}
    path.write_text(json.dumps(result, indent=2))


def judge(args: argparse.Namespace) -> int:
    out = Path(args.out)
    tasks = {t["id"]: t for t in json.loads((out / "tasks.json").read_text())}
    paths = sorted((out / "runs").glob("*.json"))
    with ThreadPoolExecutor(args.workers) as pool:
        list(
            pool.map(
                lambda p: judge_one(
                    out, Path(args.store), tasks[p.name.split(".")[0]], p, args
                ),
                paths,
            )
        )
    return 0


# ----------------------------------------------------------------- resume ---


def resume_points(bundle: Bundle, pause_seconds: int) -> list[str]:
    """Times at which a worker would start or resume on this thread.

    A point is a comment or review by a person, or a failed check, that comes
    at least ``pause_seconds`` after the previous point.
    """

    def stamp(iso: str) -> float:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()

    events = sorted((e for v in bundle.views for e in v.events), key=lambda e: e.at)
    points: list[str] = []
    for event in events:
        human = event.kind in ("opened", "comment", "review") and not event.is_bot
        failed = event.kind == "check" and event.meta.get("conclusion") in (
            "failure",
            "timed_out",
        )
        if not (human or failed):
            continue
        if not points or stamp(event.at) - stamp(points[-1]) >= pause_seconds:
            points.append(event.at)
    return points


def resume(args: argparse.Namespace) -> int:
    """Tokens a worker reads over a thread's life: reread, packet, or delta."""

    rows = []
    for repo in args.repos:
        store = ThreadStore(repo, Path(args.store))
        for number in store.numbers():
            header = store.header(number)
            full = load_bundle(store, number) if header and not header.is_pr else None
            if full is None:
                continue
            # A linked thread can be older than the root; the worker's life
            # on this thread starts when the root is opened.
            points = [
                at for at in resume_points(full, args.pause) if at >= header.created_at
            ]
            if len(points) < 2:
                continue
            totals = {"reread": 0, "packet": 0, "delta": 0, "deltas": 0, "whole": 0}
            cursor = None
            for at in points:
                bundle = load_bundle(store, number, at=at)
                packet = build_packet(bundle, budget=args.budget)
                totals["reread"] += count_tokens(dump_text(bundle))
                totals["packet"] += packet.tokens
                if cursor is None:
                    totals["delta"] += packet.tokens
                else:
                    update = build_packet(bundle, budget=args.budget, since=cursor)
                    totals["delta"] += update.tokens
                    totals["deltas"] += 1
                    totals["whole"] += "Coverage: complete." in update.text
                cursor = packet.head
            rows.append(
                {"repo": repo, "number": number, "points": len(points), **totals}
            )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "resume.json").write_text(json.dumps(rows, indent=2))
    print(f"{len(rows)} threads, pause {args.pause}s, budget {args.budget}\n")
    print("| | reread everything | packet each time | packet then deltas |")
    print("|---|---|---|---|")
    for label, pick in (
        ("median tokens per thread", statistics.median),
        ("total tokens", sum),
    ):
        cells = [pick([r[k] for r in rows]) for k in ("reread", "packet", "delta")]
        print(f"| {label} | " + " | ".join(f"{c:,.0f}" for c in cells) + " |")
    print(
        f"| median resume points | {statistics.median(r['points'] for r in rows)} | | |"
    )
    deltas = sum(r["deltas"] for r in rows)
    whole = sum(r["whole"] for r in rows)
    print(
        f"\n{whole} of {deltas} deltas carry everything people wrote since the "
        f"cursor in full ({whole / max(deltas, 1):.0%}); the rest say what they cut."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    tasks = commands.add_parser("tasks")
    tasks.add_argument("repos", nargs="+")
    tasks.add_argument("--store", required=True)
    tasks.add_argument("--clones", required=True, help="directory of <name>.git clones")
    tasks.add_argument("--out", required=True)
    tasks.add_argument("--budget", type=int, default=3000)
    tasks.add_argument("--code-budget", type=int, default=1500)
    tasks.add_argument("--max-files", type=int, default=6)
    tasks.add_argument(
        "--scenario",
        choices=("issue", "pr"),
        default="issue",
        help="issue: before its fix is opened; pr: after its first feedback",
    )
    tasks.set_defaults(handler=build_tasks)
    runner = commands.add_parser("run")
    runner.add_argument("--out", required=True)
    runner.add_argument("--arms", nargs="+", default=list(ARMS), choices=ARMS)
    runner.add_argument("--only", nargs="*")
    runner.add_argument("--limit", type=int, default=0)
    runner.add_argument(
        "--agent",
        choices=sorted(AGENTS),
        default="codex",
        help="codex: Codex CLI in its read-only sandbox; claude: Claude Code "
        "headless with sandboxed Bash and no network",
    )
    runner.add_argument("--max-budget-usd", type=float, default=3.0)
    runner.add_argument("--reps", type=int, default=1)
    runner.add_argument("--workers", type=int, default=3)
    runner.add_argument("--timeout", type=int, default=1200)
    runner.add_argument("--effort", default="medium")
    runner.add_argument("--model", default="")
    runner.add_argument("--force", action="store_true")
    runner.set_defaults(handler=run)
    scorer = commands.add_parser("score")
    scorer.add_argument("--out", required=True, nargs="+")
    scorer.set_defaults(handler=score)
    judger = commands.add_parser("judge")
    judger.add_argument("--out", required=True)
    judger.add_argument("--store", required=True)
    judger.add_argument("--workers", type=int, default=6)
    judger.add_argument("--force", action="store_true")
    judger.set_defaults(handler=judge)
    resumer = commands.add_parser("resume")
    resumer.add_argument("repos", nargs="+")
    resumer.add_argument("--store", required=True)
    resumer.add_argument("--out", required=True)
    resumer.add_argument("--budget", type=int, default=3000)
    resumer.add_argument("--pause", type=int, default=3600)
    resumer.set_defaults(handler=resume)
    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    shutil.which("git") or sys.exit("git is required")
    raise SystemExit(main())
