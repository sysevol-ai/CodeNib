# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""A task: the annotations a person sent to their agent, and what came back.

One JSON file per task under ``.codenib/tasks/``. The prompt, the agent's raw
output and the diff are logs, kept in the user's state directory instead.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..paths import repo_state_dir
from .model import AnnotationError, _atomic_write, is_id, new_id, repo_lock, utc_now

TASKS_DIR = Path(".codenib") / "tasks"

TASK_STATUSES = (
    "queued",
    "running",
    "needs_review",  # a diff waits for the person
    "done",
    "rejected",  # the person rejected the diff
    "failed",
)
TASK_KINDS = ("question", "change")


@dataclass
class Task:
    id: str
    inputs: List[str]
    kind: str  # "question": read only; "change": may edit code in a worktree
    agent: str = "claude-code"
    status: str = "queued"
    created: str = ""
    started: str = ""
    finished: str = ""
    error: str = ""
    results: List[str] = field(default_factory=list)
    attempts: int = 0
    #: For a change: ``{"branch", "base", "files", "stat", "pages"}``.
    diff: Dict[str, Any] = field(default_factory=dict)
    #: Tokens in and out, cost, turns and wall time, summed over attempts.
    usage: Dict[str, Any] = field(default_factory=dict)
    #: What the run is doing now, for the person watching it.
    progress: str = ""

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "Task":
        known = {name for name in cls.__dataclass_fields__}
        task = cls(**{k: v for k, v in data.items() if k in known})
        if not is_id(task.id):
            raise AnnotationError(f"bad task id {task.id!r}")
        return task


class TaskStore:
    def __init__(self, repo: Path | str, state: Path | str | None = None):
        self.repo = Path(repo)
        # See AnnotationStore: a visitor's tasks live in their own folder.
        self.state = Path(state) if state is not None else None
        self.directory = (
            self.state / "tasks" if self.state is not None else self.repo / TASKS_DIR
        )
        self.lock = repo_lock(self.state if self.state is not None else self.repo)

    def _path(self, task_id: str) -> Path:
        if not is_id(task_id):
            raise AnnotationError(f"bad task id {task_id!r}")
        return self.directory / f"{task_id}.json"

    def run_dir(self, task_id: str) -> Path:
        """Logs and the worktree of one task, outside the repository."""

        if not is_id(task_id):
            raise AnnotationError(f"bad task id {task_id!r}")
        if self.state is not None:
            return self.state / "runs" / task_id
        return repo_state_dir(self.repo) / "tasks" / task_id

    def get(self, task_id: str) -> Task:
        path = self._path(task_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise AnnotationError(f"no task {task_id}") from None
        return Task.from_json(data)

    def list(self) -> List[Task]:
        tasks: List[Task] = []
        if not self.directory.is_dir():
            return tasks
        for path in sorted(self.directory.glob("*.json")):
            try:
                tasks.append(Task.from_json(json.loads(path.read_text("utf-8"))))
            except (OSError, ValueError, TypeError, AnnotationError):
                continue
        return tasks

    def save(self, task: Task) -> Task:
        with self.lock:
            _atomic_write(
                self._path(task.id),
                json.dumps(task.to_json(), indent=2, ensure_ascii=False) + "\n",
            )
        return task

    def create(self, inputs: List[str], kind: str, agent: str = "claude-code") -> Task:
        if kind not in TASK_KINDS:
            raise AnnotationError(f"task kind must be one of {', '.join(TASK_KINDS)}")
        task = Task(
            id=new_id(), inputs=list(inputs), kind=kind, agent=agent, created=utc_now()
        )
        return self.save(task)

    def update(self, task_id: str, **changes: Any) -> Task:
        with self.lock:
            task = self.get(task_id)
            for key, value in changes.items():
                if key not in Task.__dataclass_fields__ or key == "id":
                    raise AnnotationError(f"cannot change {key!r}")
                setattr(task, key, value)
            return self.save(task)

    def active(self) -> Optional[Task]:
        for task in reversed(self.list()):
            if task.status in ("queued", "running"):
                return task
        return None
