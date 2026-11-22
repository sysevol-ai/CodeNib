#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Build the code walkthroughs a repository's Wiki serves.

A walkthrough follows one question through the code, one function per step:
from the call a user writes (``agent.run(task)``) to the function whose code
answers the question. Each question is curated in ``questions.yaml``: the
question, its start function, its answer function, and optionally functions
the walk must pass on the way. The build then

1. chooses the path: the cheapest chain of calls the index recorded from the
   start through each named function, with no model call (``graph_paths``);
2. places it on the Wiki: areas, call sites, crossings, citations (``layout``);
3. has the model write each step, then the title, overview and recap, each
   checked for length, marked lines and line citations (``steps``);
4. has the model check every step against its source and correct
   unsupported claims (``verify``);
5. merges the questions into ``<out-dir>/<repo_id>.json``, the file
   ``GET /api/repos/{repo_id}/walkthroughs`` serves (``served``).

Run from a CodeNib checkout, with the repository indexed under the config's
data dir, a running backend serving its Wiki (``--api``) and
``DEEPSEEK_API_KEY`` set::

    python scripts/walkthrough/build_walkthroughs.py --repo gin-gonic__gin \\
        --config qa_config.yaml

``--paths-only N`` chooses each question's path N times and prints them
(a consistency check; no text, no writes, no key needed for curated
questions). ``--keep`` also writes each question's raw walk, with its build
log, and the progress lines under ``<out-dir>/build/<repo_id>/``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import yaml

if __package__ in (None, ""):
    # Run as a file: import this checkout's scripts package (and codenib).
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codenib.web.config import load_config  # noqa: E402
from scripts.walkthrough import progress  # noqa: E402
from scripts.walkthrough.graph_paths import choose_path, path_names  # noqa: E402
from scripts.walkthrough.layout import (  # noqa: E402
    RepoMap,
    read_repo_map,
    walk_layout,
    walk_source,
)
from scripts.walkthrough.model import (  # noqa: E402
    DEFAULT_API_BASE,
    DEFAULT_MODEL,
    KEY_ENV,
    Model,
)
from scripts.walkthrough.progress import say  # noqa: E402
from scripts.walkthrough.repo_index import RepoIndex, load_repo_index  # noqa: E402
from scripts.walkthrough.served import merge_parts, write_served  # noqa: E402
from scripts.walkthrough.steps import (  # noqa: E402
    html,
    page_steps,
    write_overview,
    write_steps,
)
from scripts.walkthrough.verify import verify_walk  # noqa: E402

DEFAULT_QUESTIONS = Path(__file__).with_name("questions.yaml")
DEFAULT_API = "http://127.0.0.1:8059"
QUESTION_KEYS = ("question", "entry_call", "tab", "answer", "via")
REQUIRED_KEYS = ("id", "question", "start", "entry_call", "tab")


def load_questions(path: str | Path, repo_id: str) -> list[dict]:
    """One repository's curated questions, checked for the keys a walk needs."""

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    questions = data.get(repo_id)
    if not questions:
        raise SystemExit(f"{path} has no questions for {repo_id}")
    for qd in questions:
        missing = [k for k in REQUIRED_KEYS if k not in qd]
        if missing:
            raise SystemExit(f"{path}: question {qd.get('id')!r} lacks {missing}")
    return questions


def question_start(ix: RepoIndex, qd: dict) -> tuple[dict, dict]:
    """The question as the path and text use it, and its start function."""

    hits = ix.by_name.get(qd["start"])
    if not hits:
        raise SystemExit(f"{ix.repo_id}: no function named {qd['start']}")
    q = {k: qd[k] for k in QUESTION_KEYS if k in qd}
    return q, ix.info(hits[0])


def build_walk(ix: RepoIndex, rmap: RepoMap, qd: dict, model: Model) -> dict:
    """One question's walk, written and verified, as a one-walk file."""

    log: dict = {"calls": [], "checks": []}
    q, start = question_start(ix, qd)
    question = q["question"]
    say("question:", question, "| start:", start["name"])
    path, chosen = choose_path(ix, q, start, model, log)
    say("path:", " -> ".join(path_names(path)))
    lay = walk_layout(path, q["entry_call"], rmap)
    steps_out = write_steps(ix, question, path, lay["paths"], model, log)
    for i, st in enumerate(steps_out):
        lay["stops"][i + 1][3] = st.get("role", "")
    ov = write_overview(question, q["entry_call"], path, steps_out, model, log)
    walk = {
        "id": qd["id"],
        "tab": html(q["tab"]),
        "title": html(ov["title"]),
        "dek": html(ov["dek"]),
        "question": question,
        "chosen": chosen,
        "map": lay["map"],
        "stops": lay["stops"],
        "segs": lay["segs"],
        "zones": lay["zones"],
        "areas": lay["areas"],
        "cross": lay["cross"],
        "steps": page_steps(path, steps_out, ov, rmap.area_of_file),
    }
    data = {
        "repo": {
            "id": ix.repo_id,
            "slug": ix.repo_id.replace("__", "/"),
            "commit": ix.commit,
            "model": model.name,
        },
        "areas": rmap.areas,
        "links": rmap.links,
        "paths": lay["paths"],
        "wiki": rmap.wiki(),
        "walks": [walk],
        "source": walk_source(ix.line, path),
        "log": log,
    }
    tin = sum(c["in"] for c in log["calls"])
    tout = sum(c["out"] for c in log["calls"])
    say(f"wrote {walk['id']}: {len(log['calls'])} model calls, {tin} in / {tout} out")
    data["log"]["verify"] = verify_walk(walk, data["source"], model)
    return data


def paths_only(ix: RepoIndex, questions: list[dict], reps: int, model: Model) -> None:
    """Choose each question's path ``reps`` times and print one line per run."""

    for qd in questions:
        q, start = question_start(ix, qd)
        for rep in range(reps):
            log: dict = {"calls": [], "checks": []}
            path, _ = choose_path(ix, q, start, model, log)
            line = f"{qd['id']} #{rep + 1}: " + " -> ".join(path_names(path))
            print(line, flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build one repository's code walkthroughs for its Wiki."
    )
    parser.add_argument(
        "--repo", required=True, help="repository id, e.g. gin-gonic__gin"
    )
    parser.add_argument(
        "--questions",
        default=str(DEFAULT_QUESTIONS),
        help="curated questions, YAML keyed by repository id (default: %(default)s)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="CodeNib config YAML (default: $CODENIB_DEMO_CONFIG)",
    )
    parser.add_argument(
        "--api",
        default=DEFAULT_API,
        help="base URL of a running CodeNib backend serving the repository's Wiki "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="where <repo_id>.json goes (default: <config data_dir>/walkthroughs)",
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL, help="litellm model (default: %(default)s)"
    )
    parser.add_argument(
        "--model-api-base",
        default=DEFAULT_API_BASE,
        help="the model's API base URL (default: %(default)s)",
    )
    parser.add_argument(
        "--think",
        action="store_true",
        help="let the model reason before planning decisions (questions without "
        "a reachable answer only)",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="also write each question's raw walk and the build log under "
        "<out-dir>/build/<repo_id>/",
    )
    parser.add_argument(
        "--paths-only",
        type=int,
        metavar="N",
        default=0,
        help="choose each question's path N times and print them; no text, no writes",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # The index and Wiki code log freely; progress lines go to stderr instead.
    logging.disable(logging.WARNING)
    questions = load_questions(args.questions, args.repo)
    model = Model(args.model, args.model_api_base, think=args.think)
    if not args.paths_only and not os.environ.get(KEY_ENV):
        raise SystemExit(f"set {KEY_ENV} to write walkthrough text")
    cfg = load_config(args.config)
    ix = load_repo_index(cfg, args.repo)
    if args.paths_only:
        paths_only(ix, questions, args.paths_only, model)
        return 0

    out_dir = Path(args.out_dir or Path(os.path.abspath(cfg.data_dir)) / "walkthroughs")
    keep_dir = out_dir / "build" / args.repo if args.keep else None
    log_file = None
    if keep_dir is not None:
        keep_dir.mkdir(parents=True, exist_ok=True)
        log_file = open(keep_dir / "build.log", "w", encoding="utf-8")
        progress.add_log(log_file)
    try:
        rmap = read_repo_map(ix.outline, args.api, args.repo)
        parts = []
        for qd in questions:
            say(f"===== {args.repo} {qd['id']}")
            part = build_walk(ix, rmap, qd, model)
            if keep_dir is not None:
                raw = keep_dir / f"{qd['id']}.json"
                raw.write_text(json.dumps(part, indent=1), encoding="utf-8")
            parts.append(part)
        target = write_served(merge_parts(parts), out_dir)
        say("wrote", target, target.stat().st_size, [qd["id"] for qd in questions])
    finally:
        if log_file is not None:
            progress.remove_log(log_file)
            log_file.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
