# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Annotations on the Wiki: questions, change requests and notes people leave
on a page, the tasks that send them to a coding agent, and the answers the
agent writes back next to them.

Everything is a plain file in the repository:

- ``.codenib/notes/<id>.md``: one annotation, in the guard's note format
  (front matter, then the text) with more keys;
- ``.codenib/tasks/<id>.json``: one dispatched task and what came of it.

Run logs and the agent's worktrees live in the user's state directory, outside
the repository.
"""

from .model import (
    AGENT_KINDS,
    ANNOTATION_KINDS,
    HUMAN_KINDS,
    Annotation,
    AnnotationError,
    AnnotationStore,
)
from .tasks import Task, TaskStore

__all__ = [
    "AGENT_KINDS",
    "ANNOTATION_KINDS",
    "HUMAN_KINDS",
    "Annotation",
    "AnnotationError",
    "AnnotationStore",
    "Task",
    "TaskStore",
]
