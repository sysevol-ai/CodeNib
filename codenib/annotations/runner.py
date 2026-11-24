# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""The task loop: dispatch annotations, run the agent, write its results back.

1. ``dispatch`` turns draft annotations into one task.
2. ``run_task`` starts the agent with every annotation and its context in the
   prompt. A question runs read-only in the repository. A change runs in a git
   worktree on a branch of its own, so the person's working tree is untouched.
3. Every annotation needs a result. Missing ones are asked for once more.
4. Results become agent annotations under the ones they answer. A change also
   leaves a short note on every other Wiki page that cites a file it edited.
5. ``accept`` applies a change's diff to the person's working tree; ``reject``
   drops it.

The hosted Wiki serves one shared, read-only checkout. There each visitor's
annotations and tasks live in a ``state`` folder of their own, a change runs in
a private clone (``shared=True``), and accepting hunks only records them: the
person takes the change away as a patch.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .agents import Agent, AgentRun, add_usage
from .hunks import all_hunks, parse_patch, patch_for
from .model import Annotation, AnnotationError, AnnotationStore, utc_now
from .prompt import RESULT_SCHEMA, SYSTEM_PROMPT, PageLookup, build_prompt
from .tasks import Task, TaskStore

log = logging.getLogger(__name__)

#: Changed line ranges by path (in the base commit) -> the pages whose
#: citations reach them: ``[{"id", "title", "files"}]``.
PagesCiting = Callable[[Dict[str, List[Tuple[int, int]]]], List[Dict[str, Any]]]
DISPATCHABLE = ("draft", "failed", "rejected")
_EXCLUDE = ":(exclude).codenib"
_MAX_PATCH_BYTES = 2_000_000
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def _git(repo: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AnnotationError(
            completed.stderr.strip() or f"git {args[0]} exited {completed.returncode}"
        )
    return completed.stdout


def branch_name(task_id: str) -> str:
    return f"codenib/task-{task_id.lower()}"


def dispatch(
    repo: Path,
    annotation_ids: Sequence[str],
    *,
    agent: str = "claude-code",
    state: Optional[Path] = None,
) -> Task:
    """Make one task of these annotations and mark them dispatched."""

    store, tasks = AnnotationStore(repo, state), TaskStore(repo, state)
    ids = list(dict.fromkeys(annotation_ids))
    if not ids:
        raise AnnotationError("select at least one annotation to dispatch")
    with store.lock:
        if tasks.active() is not None:
            raise AnnotationError("a task is already running; wait for it to finish")
        annotations = [store.get(i) for i in ids]
        for annotation in annotations:
            if annotation.author != "human" or annotation.kind not in (
                "question",
                "change",
            ):
                raise AnnotationError(
                    f"{annotation.id} is a {annotation.kind}; only questions and "
                    "change requests are dispatched"
                )
            if annotation.status not in DISPATCHABLE:
                raise AnnotationError(f"{annotation.id} is already {annotation.status}")
        kind = "change" if any(a.kind == "change" for a in annotations) else "question"
        task = tasks.create(ids, kind, agent=agent)
        for annotation in annotations:
            store.update(annotation.id, status="dispatched", task=task.id)
    return task


def _match_results(
    structured: Optional[Dict[str, Any]], inputs: Sequence[Annotation]
) -> Dict[str, Dict[str, Any]]:
    """Results by annotation id; an id the agent mangled is matched by content."""

    items = (structured or {}).get("results")
    if not isinstance(items, list):
        return {}
    wanted = {a.id for a in inputs}
    matched: Dict[str, Dict[str, Any]] = {}
    loose: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or not str(item.get("answer") or "").strip():
            continue
        key = str(item.get("annotation") or "").strip().upper()
        hit = key if key in wanted else next((i for i in wanted if i in key), None)
        if hit and hit not in matched:
            matched[hit] = item
        else:
            loose.append(item)
    # One input and one unmatched answer: the agent answered it under a bad id.
    rest = [a.id for a in inputs if a.id not in matched]
    if len(rest) == 1 and len(loose) == 1:
        matched[rest[0]] = loose[0]
    return matched


def _existing_files(root: Path, files: Any) -> List[Dict[str, Any]]:
    kept = []
    for item in files if isinstance(files, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        path = re.sub(r":\d+(-\d+)?$", "", item["path"].strip())
        prefix = str(root).rstrip("/") + "/"
        if path.startswith(prefix):
            path = path[len(prefix) :]
        while path.startswith("./"):
            path = path[2:]
        relative = Path(path)
        if (
            not path
            or relative.is_absolute()
            or ".." in relative.parts
            or not (root / relative).is_file()
        ):
            continue
        entry: Dict[str, Any] = {"path": path}
        for key in ("start_line", "end_line"):
            value = item.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                entry[key] = value
        kept.append(entry)
    return kept


def _diff(worktree: Path, base: str) -> Dict[str, Any]:
    _git(worktree, "add", "-A", "--", ".", _EXCLUDE)
    patch = _git(worktree, "diff", "--cached", "--binary", base, "--", ".", _EXCLUDE)
    if not patch.strip():
        return {}
    files = []
    for line in _git(
        worktree, "diff", "--cached", "--numstat", base, "--", ".", _EXCLUDE
    ).splitlines():
        added, deleted, path = (line.split("\t", 2) + ["", "", ""])[:3]
        files.append(
            {
                "path": path,
                "added": int(added) if added.isdigit() else 0,
                "deleted": int(deleted) if deleted.isdigit() else 0,
            }
        )
    return {"patch": patch, "files": files, "ranges": changed_ranges(patch)}


def changed_ranges(patch: str) -> Dict[str, List[Tuple[int, int]]]:
    """The lines each file's change touches, numbered as in the base commit.

    Only changed lines count, not the context around them. A removed line is
    itself; an inserted line touches the two lines it falls between.
    """

    marks: Dict[str, List[int]] = {}
    path = ""
    header = False
    replacing = False
    old = 0
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            header, path = True, ""
        elif header and line.startswith("--- "):
            name = line[4:].strip()
            path = name[2:] if name.startswith("a/") else ""
        elif header and line.startswith("+++ "):
            name = line[4:].strip()
            if not path and name.startswith("b/"):
                path = name[2:]
        elif path and (match := _HUNK.match(line)):
            header, replacing = False, False
            old = int(match.group(1))
            if match.group(2) == "0":
                old += 1  # an insertion at the top: the hunk names the line before
        elif path and not header:
            if line.startswith("+"):
                # Lines added right after removed ones replace them; only a
                # pure insertion touches its neighbours.
                if not replacing:
                    marks.setdefault(path, []).extend({max(old - 1, 1), max(old, 1)})
            elif line.startswith("-"):
                marks.setdefault(path, []).append(old)
                old += 1
                replacing = True
            elif line.startswith(" "):
                old += 1
                replacing = False
    ranges: Dict[str, List[Tuple[int, int]]] = {}
    for name, lines in marks.items():
        spans: List[Tuple[int, int]] = []
        for number in sorted(set(lines)):
            if spans and number <= spans[-1][1] + 1:
                spans[-1] = (spans[-1][0], number)
            else:
                spans.append((number, number))
        ranges[name] = spans
    return ranges


def _remove_worktree(repo: Path, worktree: Path, shared: bool = False) -> None:
    if shared:
        # A private clone: the shared checkout knows nothing about it.
        shutil.rmtree(worktree, ignore_errors=True)
        return
    if worktree.exists():
        _git(repo, "worktree", "remove", "--force", str(worktree), check=False)
        shutil.rmtree(worktree, ignore_errors=True)
    _git(repo, "worktree", "prune", check=False)


def _make_workspace(
    repo: Path, worktree: Path, base: str, task_id: str, shared: bool
) -> None:
    _remove_worktree(repo, worktree, shared)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if shared:
        # Borrow the checkout's objects, write nothing into it.
        _git(
            worktree.parent,
            "clone",
            "-q",
            "--shared",
            "--no-checkout",
            str(repo),
            str(worktree),
        )
        _git(worktree, "checkout", "-q", "--detach", base)
        return
    _git(
        repo,
        "worktree",
        "add",
        "-q",
        "-b",
        branch_name(task_id),
        str(worktree),
        base,
    )


def _drop_branch(repo: Path, task_id: str, shared: bool) -> None:
    if not shared:
        _git(repo, "branch", "-D", branch_name(task_id), check=False)


def run_task(
    repo: Path,
    task_id: str,
    *,
    agent: Agent,
    page_lookup: PageLookup,
    pages_citing: Optional[PagesCiting] = None,
    repo_name: str = "",
    state: Optional[Path] = None,
    shared: bool = False,
) -> Task:
    """Run one task to the end; never raises for a failure of the agent."""

    store, tasks = AnnotationStore(repo, state), TaskStore(repo, state)
    task = tasks.update(
        task_id, status="running", started=utc_now(), progress="Starting"
    )
    run_dir = tasks.run_dir(task.id)
    worktree = run_dir / "worktree"
    edit = task.kind == "change"
    base = ""
    try:
        inputs = [store.get(i) for i in task.inputs]
        cwd = repo
        if edit:
            base = _git(repo, "rev-parse", "HEAD").strip()
            _make_workspace(repo, worktree, base, task.id, shared)
            cwd = worktree
        results: Dict[str, Dict[str, Any]] = {}
        usage: Dict[str, Any] = {}
        last_error = ""
        pending = list(inputs)
        for attempt in (1, 2):
            note = ""
            if attempt == 2:
                note = (
                    "Your previous run returned no usable result for the annotations "
                    "below. Handle only these, and return one result for each."
                )
            label = getattr(agent, "label", agent.name)
            doing = "editing a worktree" if edit else "reading the code"
            tasks.update(
                task.id,
                attempts=attempt,
                progress=(
                    f"{label} is {doing}"
                    if attempt == 1
                    else f"{label} is answering the {len(pending)} it missed"
                ),
            )
            prompt = build_prompt(
                pending, store, page_lookup, repo_name=repo_name, retry_note=note
            )
            run: AgentRun = agent.run(
                prompt,
                cwd=cwd,
                system=SYSTEM_PROMPT,
                schema=RESULT_SCHEMA,
                edit=edit,
                log_dir=run_dir,
            )
            usage = add_usage(usage, run.usage)
            last_error = run.error
            results.update(_match_results(run.structured, pending))
            pending = [a for a in inputs if a.id not in results]
            if not pending:
                break
        tasks.update(task.id, usage=usage, progress="Writing results back")

        diff: Dict[str, Any] = {}
        if edit:
            found = _diff(worktree, base)
            if found:
                _git(
                    worktree,
                    "-c",
                    "user.name=CodeNib",
                    "-c",
                    "user.email=codenib@localhost",
                    "commit",
                    "-q",
                    "--no-verify",
                    "-m",
                    f"CodeNib task {task.id}",
                )
                patch = found.pop("patch")
                ranges = found.pop("ranges")
                (run_dir / "diff.patch").write_text(patch, encoding="utf-8")
                branch = "" if shared else branch_name(task.id)
                diff = {"branch": branch, "base": base, **found}
                if pages_citing:
                    try:
                        diff["pages"] = pages_citing(ranges)
                    except Exception:  # noqa: BLE001 - placement is a bonus
                        log.exception("could not place task %s on pages", task.id)

        written: List[str] = []
        for annotation in inputs:
            result = results.get(annotation.id)
            if result is None:
                store.update(annotation.id, status="failed")
                continue
            reply = store.create(
                kind="answer" if annotation.kind == "question" else "summary",
                text=str(result["answer"]).strip(),
                on=annotation.on,
                author="agent",
                status="open",
                reply_to=annotation.id,
                task=task.id,
                files=_existing_files(cwd, result.get("files")),
            )
            written.append(reply.id)
            if annotation.kind == "question" or not diff:
                store.update(annotation.id, status="answered")
            else:
                store.update(annotation.id, status="needs_review")
        origin_pages = {a.page for a in inputs}
        origin = next(iter(sorted(p for p in origin_pages if p)), "")
        origin_title = origin
        if origin:
            try:
                origin_title = str((page_lookup(origin) or {}).get("title") or origin)
            except Exception:  # noqa: BLE001 - the id is still a usable link
                pass
        for page in diff.get("pages") or []:
            if page.get("id") in origin_pages:
                continue
            files = ", ".join(f"`{f}`" for f in sorted(page.get("files") or []))
            where = f"[{origin_title}](?p={origin})" if origin else "another page"
            # The note points back to the request; the diff is reviewed there.
            note = store.create(
                kind="summary",
                text=(
                    f"A change requested on {where} edits lines of "
                    f"{files or 'the code'} that this page cites. Review it on "
                    "that page before it is applied."
                ),
                on={"page": str(page["id"])},
                author="agent",
                status="open",
                task=task.id,
                files=[{"path": f} for f in page.get("files") or []],
            )
            written.append(note.id)

        if not results:
            status = "failed"
            error = last_error or "the agent returned no result"
        else:
            status = "needs_review" if diff else "done"
            error = (
                ""
                if not pending
                else (f"no result for {len(pending)} of {len(inputs)} annotations")
            )
        if edit and (shared or not diff):
            # A private clone is not needed once its diff is saved.
            _remove_worktree(repo, worktree, shared)
            if not diff:
                _drop_branch(repo, task.id, shared)
        return tasks.update(
            task.id,
            status=status,
            error=error,
            results=written,
            diff=diff,
            finished=utc_now(),
            progress="",
        )
    except Exception as exc:  # noqa: BLE001 - a task must end in a state
        log.exception("task %s failed", task_id)
        for annotation_id in task.inputs:
            try:
                store.update(annotation_id, status="failed")
            except AnnotationError:
                pass
        if edit:
            _remove_worktree(repo, worktree, shared)
            _drop_branch(repo, task.id, shared)
        return tasks.update(
            task.id,
            status="failed",
            error=str(exc) or exc.__class__.__name__,
            finished=utc_now(),
            progress="",
        )


def task_patch(repo: Path, task_id: str, state: Optional[Path] = None) -> str:
    path = TaskStore(repo, state).run_dir(task_id) / "diff.patch"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    return text[:_MAX_PATCH_BYTES]


def _close_change(
    repo: Path,
    task: Task,
    status: str,
    state: Optional[Path] = None,
    shared: bool = False,
) -> None:
    store = AnnotationStore(repo, state)
    for annotation_id in task.inputs + task.results:
        try:
            annotation = store.get(annotation_id)
        except AnnotationError:
            continue
        if annotation.kind in ("change", "summary"):
            store.update(annotation_id, status=status)
    tasks = TaskStore(repo, state)
    _remove_worktree(repo, tasks.run_dir(task.id) / "worktree", shared)


def review(repo: Path, task_id: str, state: Optional[Path] = None) -> Dict[str, Any]:
    """A change's diff, hunk by hunk, with what was decided about each."""

    task = TaskStore(repo, state).get(task_id)
    patch = task_patch(repo, task_id, state)
    accepted = set(task.diff.get("accepted") or [])
    rejected = set(task.diff.get("rejected") or [])
    hunks = []
    for hunk in all_hunks(parse_patch(patch)):
        state = (
            "accepted"
            if hunk.id in accepted
            else "rejected" if hunk.id in rejected else "open"
        )
        hunks.append({**hunk.to_json(), "state": state, "lines": hunk.lines[:2000]})
    return {"patch": patch, "hunks": hunks}


def _decide(
    repo: Path,
    task_id: str,
    hunks: Optional[Sequence[str]],
    decision: str,
    state: Optional[Path] = None,
    shared: bool = False,
) -> Task:
    tasks = TaskStore(repo, state)
    with tasks.lock:
        task = tasks.get(task_id)
        if task.status != "needs_review":
            raise AnnotationError(
                f"task {task_id} is {task.status}, not waiting for review"
            )
        path = tasks.run_dir(task.id) / "diff.patch"
        if not path.is_file():
            raise AnnotationError("the diff of this task is gone")
        files = parse_patch(path.read_text(encoding="utf-8"))
        every = [hunk.id for hunk in all_hunks(files)]
        accepted = list(task.diff.get("accepted") or [])
        rejected = list(task.diff.get("rejected") or [])
        undecided = [i for i in every if i not in accepted and i not in rejected]
        chosen = list(dict.fromkeys(hunks)) if hunks else undecided
        for hunk_id in chosen:
            if hunk_id not in every:
                raise AnnotationError(f"this change has no hunk {hunk_id}")
            if hunk_id not in undecided:
                raise AnnotationError(f"hunk {hunk_id} was already decided")
        if decision == "accept" and chosen and shared:
            # Nothing to apply to: the kept hunks become the patch to take.
            accepted += chosen
        elif decision == "accept" and chosen:
            partial = tasks.run_dir(task.id) / f"accept-{len(accepted)}.patch"
            partial.write_text(patch_for(files, chosen), encoding="utf-8")
            completed = subprocess.run(
                ["git", "-C", str(repo), "apply", "--check", "--recount", str(partial)],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise AnnotationError(
                    "The change no longer applies to your working tree: "
                    + (completed.stderr.strip().splitlines() or ["conflict"])[0]
                )
            _git(repo, "apply", "--recount", str(partial))
            accepted += chosen
        else:
            rejected += chosen
        diff = {**task.diff, "accepted": accepted, "rejected": rejected}
        if any(i not in accepted and i not in rejected for i in every):
            return tasks.update(task.id, diff=diff)
        # Every hunk is decided: the change is applied if any part of it was.
        _close_change(repo, task, "applied" if accepted else "rejected", state, shared)
        if not accepted:
            _drop_branch(repo, task.id, shared)
        return tasks.update(
            task.id,
            diff=diff,
            status="done" if accepted else "rejected",
            finished=utc_now(),
        )


def accept(
    repo: Path,
    task_id: str,
    hunks: Optional[Sequence[str]] = None,
    state: Optional[Path] = None,
    shared: bool = False,
) -> Task:
    """Apply hunks of a change to the working tree; all undecided ones by default.

    With ``shared`` the hunks are only recorded as kept (see ``kept_patch``).
    """

    return _decide(repo, task_id, hunks, "accept", state, shared)


def reject(
    repo: Path,
    task_id: str,
    hunks: Optional[Sequence[str]] = None,
    state: Optional[Path] = None,
    shared: bool = False,
) -> Task:
    """Discard hunks of a change; all undecided ones by default.

    When nothing of the change was applied, its branch goes too; the record
    stays.
    """

    return _decide(repo, task_id, hunks, "reject", state, shared)


def kept_patch(repo: Path, task_id: str, state: Optional[Path] = None) -> str:
    """The change without the hunks a person discarded."""

    task = TaskStore(repo, state).get(task_id)
    rejected = set(task.diff.get("rejected") or [])
    files = parse_patch(task_patch(repo, task_id, state))
    return patch_for(files, [h.id for h in all_hunks(files) if h.id not in rejected])
