# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Build a window of per-commit symbol graphs using incremental patching.

Produces the artifacts behind the web demo's commit selector: one graph
snapshot per commit for the last ``--n`` first-parent commits of a ref.

Only the *oldest* commit in the window pays a full cold (SCIP) build. Every
later commit is reached by patching the previous graph forward through the
LSP-backed incremental patcher -- the mechanism this project's incremental
maintenance results are about. That is also why the window is walked
oldest-to-newest even though the UI presents it newest-first: the patcher only
moves forward from an existing base graph.

The repository is never checked out in place. A dedicated detached ``git
worktree`` is created and advanced commit by commit, so a live checkout being
served (or actively developed in) is untouched.

Example::

    python scripts/build_commit_window.py \\
        --repo-dir /home/intern/yashJ/CodeMiner \\
        --n 5 --ref origin/main --language paper

``--language`` takes a comma-separated list; ``paper`` expands to the languages
the incremental-maintenance experiment covers (go, python, rust, typescript).
Languages that are not buildable in the target repo are dropped with a warning.

Requires each language's LSP on PATH (Python: ``basedpyright-langserver`` via
``make python-lsp-tool``; TypeScript: ``typescript-language-server``).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from typing import List, Optional

logger = logging.getLogger("build_commit_window")

# Written next to the snapshots; the web layer reads this to populate the
# commit selector without re-shelling out to git.
MANIFEST_NAME = "commit_window.json"


@dataclass
class CommitEntry:
    """One selectable point in the window."""

    sha: str
    short: str
    subject: str
    date: str
    author: str
    graph_path: str
    # "cold" for the anchor, "patched" for every incrementally reached commit.
    method: str
    build_seconds: float
    node_count: int
    edge_count: int
    # Files the transition touched (empty for the anchor).
    changed_files: int
    # Populated only when a patch reports stats.
    patch_stats: Optional[dict] = None


def _git(repo: str, *args: str) -> str:
    """Run git in *repo* and return stripped stdout."""
    out = subprocess.check_output(
        ["git", "-C", repo, *args],
        text=True,
        stderr=subprocess.PIPE,
    )
    return out.strip()


def resolve_window(repo: str, ref: str, n: int) -> List[dict]:
    """Return the last *n* first-parent commits of *ref*, newest first."""
    sep = "\x1f"
    fmt = sep.join(["%H", "%h", "%s", "%ad", "%an"])
    raw = _git(
        repo,
        "log",
        "--first-parent",
        f"-{n}",
        f"--format={fmt}",
        "--date=short",
        ref,
    )
    commits = []
    for line in raw.splitlines():
        sha, short, subject, date, author = line.split(sep)
        commits.append(
            {
                "sha": sha,
                "short": short,
                "subject": subject,
                "date": date,
                "author": author,
            }
        )
    return commits


def ensure_worktree(repo: str, path: str, sha: str) -> None:
    """Create (or reset) a detached worktree of *repo* at *sha*.

    Never touches ``repo``'s own working tree.
    """
    if os.path.isdir(os.path.join(path, ".git")) or os.path.isfile(
        os.path.join(path, ".git")
    ):
        logger.info("reusing worktree %s", path)
        _git(repo, "worktree", "repair", path)
        subprocess.check_call(
            ["git", "-C", path, "checkout", "--detach", "--force", sha],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Drop any stray files a previous patch run left behind so the tree is
        # a faithful checkout of `sha`.
        subprocess.check_call(
            ["git", "-C", path, "clean", "-qfdx"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # A previous run whose directory was deleted out from under git leaves a
    # "prunable" registration behind, and `worktree add` refuses that path until
    # it is cleared.
    subprocess.call(
        ["git", "-C", repo, "worktree", "prune"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    logger.info("creating worktree %s @ %s", path, sha[:8])
    subprocess.check_call(
        ["git", "-C", repo, "worktree", "add", "--detach", path, sha],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def advance_worktree(path: str, sha: str) -> None:
    """Move the worktree to *sha* (detached, discarding local state)."""
    subprocess.check_call(
        ["git", "-C", path, "checkout", "--detach", "--force", sha],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _counts(graph) -> tuple:
    try:
        return len(graph.graph.vs), len(graph.graph.es)
    except Exception:  # noqa: BLE001 - counts are informational only
        return 0, 0


# Languages the paper's incremental-maintenance experiment covers. C/C++ has a
# patcher but is absent from that experiment, so it is not included here.
PAPER_INCREMENTAL_LANGUAGES = ("go", "python", "rust", "typescript")


def parse_languages(raw: str) -> List[str]:
    """Expand the --language value into an ordered, de-duplicated list."""
    if raw.strip().lower() in {"paper", "all"}:
        return list(PAPER_INCREMENTAL_LANGUAGES)
    out: List[str] = []
    for part in raw.split(","):
        name = part.strip().lower()
        if name and name not in out:
            out.append(name)
    return out


# ls_router reports an unbuildable language as
# "Failed to build code graph for language 'go' under '<path>'".
_LANG_FAILURE = re.compile(r"language '([^']+)'")


def build_anchor(worktree: str, out_dir: str, languages: List[str], graph_route: str):
    """Cold-build the oldest commit's graph, mirroring SymbolGraphBuilder.

    A language whose cold build fails is dropped and the build retried with the
    rest. This mirrors ``IndexCompiler``'s treatment of optional builders: a
    repository that merely *contains* a few Go or Rust files but is not a Go or
    Rust project (no ``go.mod`` / ``Cargo.toml``) should still get a graph for
    the languages it really is written in, rather than no graph at all.

    Returns ``(graph, languages_actually_built)``.
    """
    from codeminer.ls_router import build_graph_for_languages

    os.makedirs(out_dir, exist_ok=True)
    remaining = list(languages)
    dropped: List[str] = []

    while remaining:
        try:
            graph = build_graph_for_languages(
                worktree,
                out_dir,
                languages=list(remaining),
                project_name=os.path.basename(os.path.abspath(worktree)),
                skip_level=None,
                graph_route=graph_route,
            )
        except RuntimeError as exc:
            match = _LANG_FAILURE.search(str(exc))
            failed = match.group(1) if match else None
            if failed is None or failed not in remaining:
                # Not an isolatable per-language failure: surface it.
                raise
            remaining.remove(failed)
            dropped.append(failed)
            logger.warning(
                "cold build: %r not buildable here (%s); continuing without it",
                failed,
                str(exc).splitlines()[0][:120],
            )
            continue

        if graph is None or not hasattr(graph, "graph"):
            raise RuntimeError("cold graph build returned no graph")
        if len(graph.graph.vs) == 0:
            raise RuntimeError("cold graph build returned an empty graph")
        if dropped:
            logger.warning("cold build skipped language(s): %s", ", ".join(dropped))
        return graph, remaining

    raise RuntimeError(
        f"no language could be cold-built here (tried: {', '.join(languages)})"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo-dir", required=True, help="git repo to read history from")
    ap.add_argument(
        "--n", type=int, default=5, help="commits in the window (default 5)"
    )
    ap.add_argument("--ref", default="HEAD", help="ref to walk (default HEAD)")
    ap.add_argument(
        "--language",
        default="python",
        help=(
            "comma-separated graph/patcher languages. Defaults to python; "
            "'--language paper' expands to the languages evaluated by the "
            "incremental-maintenance experiment (go,python,rust,typescript)."
        ),
    )
    ap.add_argument(
        "--out-dir",
        default=None,
        help="where snapshots land (default <repo>/.codeminer_cache/commit_window)",
    )
    ap.add_argument(
        "--worktree",
        default=None,
        help="worktree path (default <out-dir>/_worktree)",
    )
    ap.add_argument(
        "--graph-route", default="active", help="graph route (default: active)"
    )
    ap.add_argument(
        "--keep-worktree",
        action="store_true",
        help="leave the worktree on disk (default: remove when finished)",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    repo = os.path.abspath(args.repo_dir)
    out_dir = os.path.abspath(
        args.out_dir or os.path.join(repo, ".codeminer_cache", "commit_window")
    )
    worktree = os.path.abspath(args.worktree or os.path.join(out_dir, "_worktree"))
    os.makedirs(out_dir, exist_ok=True)

    if args.n < 1:
        logger.error("--n must be >= 1")
        return 2

    languages = parse_languages(args.language)
    if not languages:
        logger.error("--language resolved to no languages")
        return 2
    logger.info("languages: %s", ", ".join(languages))

    # Newest first (UI order); we build oldest first (patcher order).
    window = resolve_window(repo, args.ref, args.n)
    if not window:
        logger.error("no commits found for ref %r", args.ref)
        return 2
    chain = list(reversed(window))
    anchor = chain[0]

    logger.info(
        "window: %d commits %s..%s (anchor %s)",
        len(chain),
        chain[0]["short"],
        chain[-1]["short"],
        anchor["short"],
    )

    from codeminer.graph.incremental.graph_patcher import (
        LANGUAGE_EXTENSIONS,
        GraphPatcher,
    )

    ensure_worktree(repo, worktree, anchor["sha"])

    entries: List[CommitEntry] = []

    # --- anchor: the one cold build -------------------------------------
    anchor_out = os.path.join(out_dir, f"graph_{anchor['short']}")
    t0 = time.time()
    logger.info(
        "[1/%d] cold build @ %s (%s)",
        len(chain),
        anchor["short"],
        anchor["subject"][:60],
    )
    graph, languages = build_anchor(worktree, anchor_out, languages, args.graph_route)
    cold_s = time.time() - t0
    anchor_pkl = os.path.join(out_dir, f"graph_{anchor['short']}.pkl")
    graph.save_graph(anchor_pkl)
    nv, ne = _counts(graph)
    logger.info("      cold build %.1fs -> %d nodes / %d edges", cold_s, nv, ne)
    entries.append(
        CommitEntry(
            sha=anchor["sha"],
            short=anchor["short"],
            subject=anchor["subject"],
            date=anchor["date"],
            author=anchor["author"],
            graph_path=anchor_pkl,
            method="cold",
            build_seconds=round(cold_s, 3),
            node_count=nv,
            edge_count=ne,
            changed_files=0,
        )
    )

    # --- patch forward through the rest of the chain ---------------------
    # One patcher (and one language server) per language, all sharing the same
    # CodeGraph: each patches its own slice of a transition in place. A language
    # whose server will not start is dropped rather than failing the window --
    # its files simply stop contributing graph updates.
    patchers: List[tuple] = []
    lsp_startup = 0.0
    for lang in languages:
        exts = LANGUAGE_EXTENSIONS.get(lang)
        if not exts:
            logger.warning("no graph extensions registered for %r; skipping", lang)
            continue
        patcher = GraphPatcher(
            project_root=worktree,
            code_graph=graph,
            language=lang,
        )
        lsp_t0 = time.time()
        try:
            patcher.start_lsp()
        except (
            Exception
        ) as exc:  # noqa: BLE001 - a missing server disables one language
            logger.warning(
                "%s: LSP failed to start (%s: %s); this language will not be patched",
                lang,
                type(exc).__name__,
                exc,
            )
            continue
        took = time.time() - lsp_t0
        lsp_startup += took
        patchers.append((lang, patcher, exts))
        logger.info("      LSP[%s] started in %.1fs", lang, took)

    if not patchers:
        logger.error("no language server started; cannot patch the chain")
        return 1
    logger.info(
        "      %d language server(s) up in %.1fs total, amortized over %d transitions",
        len(patchers),
        lsp_startup,
        max(len(chain) - 1, 1),
    )

    try:
        for i, commit in enumerate(chain[1:], start=2):
            prev = chain[i - 2]
            logger.info(
                "[%d/%d] patch %s -> %s (%s)",
                i,
                len(chain),
                prev["short"],
                commit["short"],
                commit["subject"][:60],
            )
            # Detect per language first so we know whether this transition
            # touches anything we can patch at all.
            per_lang = []
            n_changed = 0
            for lang, patcher, exts in patchers:
                changed = GraphPatcher.detect_changed_files(
                    worktree, prev["sha"], commit["sha"], extensions=exts
                )
                count = sum(len(v) for k, v in changed.items() if k != "renamed") + len(
                    changed.get("renamed", [])
                )
                if count:
                    per_lang.append((lang, patcher, changed, count))
                    n_changed += count

            # The patcher reads file bodies off disk, so the tree must be at
            # the target commit before we sync files into any language server.
            advance_worktree(worktree, commit["sha"])

            t1 = time.time()
            stats: dict = {}
            for lang, patcher, changed, count in per_lang:
                try:
                    # earlier_commit lets the patcher pull pre-edit file bodies
                    # from git to build its line-shift map; post-edit bodies
                    # come from the worktree we just advanced.
                    lang_stats = patcher.patch_files(
                        changed,
                        earlier_commit=prev["sha"],
                        later_commit=commit["sha"],
                    )
                except Exception as exc:  # noqa: BLE001 - keep the window building
                    logger.warning(
                        "      %s: patch failed (%s: %s); leaving its facts unchanged",
                        lang,
                        type(exc).__name__,
                        exc,
                    )
                    continue
                stats[lang] = {"changed_files": count, "stats": lang_stats}
                logger.info("        %-11s %d file(s)", lang, count)
            patch_s = time.time() - t1

            pkl = os.path.join(out_dir, f"graph_{commit['short']}.pkl")
            graph.save_graph(pkl)
            nv, ne = _counts(graph)
            logger.info(
                "      %d changed file(s), patch %.2fs -> %d nodes / %d edges",
                n_changed,
                patch_s,
                nv,
                ne,
            )
            entries.append(
                CommitEntry(
                    sha=commit["sha"],
                    short=commit["short"],
                    subject=commit["subject"],
                    date=commit["date"],
                    author=commit["author"],
                    graph_path=pkl,
                    method="patched",
                    build_seconds=round(patch_s, 3),
                    node_count=nv,
                    edge_count=ne,
                    changed_files=n_changed,
                    patch_stats=stats if isinstance(stats, dict) else None,
                )
            )
    finally:
        for lang, patcher, _ in patchers:
            try:
                patcher.stop_lsp()
            except Exception as exc:  # noqa: BLE001 - shutdown is best-effort
                logger.warning("stop_lsp[%s]: %s", lang, exc)

    # UI order is newest-first; entries were produced oldest-first.
    entries.reverse()
    manifest = {
        "repo_dir": repo,
        "ref": args.ref,
        "languages": languages,
        "language": languages[0] if languages else None,
        "graph_route": args.graph_route,
        "generated_at": time.time(),
        "lsp_startup_seconds": round(lsp_startup, 3),
        "commits": [asdict(e) for e in entries],
    }
    manifest_path = os.path.join(out_dir, MANIFEST_NAME)
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    if not args.keep_worktree:
        logger.info("removing worktree %s", worktree)
        shutil.rmtree(worktree, ignore_errors=True)
        subprocess.call(
            ["git", "-C", repo, "worktree", "prune"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    cold = next((e for e in entries if e.method == "cold"), None)
    patched = [e for e in entries if e.method == "patched"]
    logger.info("wrote %s", manifest_path)
    if cold and patched:
        avg_patch = sum(e.build_seconds for e in patched) / len(patched)
        logger.info(
            "cold %.1fs vs mean patch %.2fs over %d transitions (%.1fx)",
            cold.build_seconds,
            avg_patch,
            len(patched),
            (cold.build_seconds / avg_patch) if avg_patch else float("nan"),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
