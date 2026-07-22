# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Append-only storage for anonymous visitor feedback.

Feedback is written as JSON Lines under the demo's ``data_dir``, alongside the
other durable demo state. That choice is deliberate for a publicly reachable
deployment:

* no credentials live on the server, so nothing can leak from it;
* no outbound network call is made while handling a request;
* the file is trivially greppable and survives restarts.

Triage happens out of band: ``scripts/export_feedback.py`` reads this file so a
human can review entries before anything is filed upstream. Wiring the request
handler directly to an issue tracker would require a write-scoped token on a
public host and would expose that tracker to unmoderated submissions.

Nothing identifying is recorded -- no IP address, no email, no cookie.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

FEEDBACK_FILENAME = "feedback.jsonl"

# Bounds, enforced server-side. The client also limits input, but a public
# endpoint must not trust that.
MAX_MESSAGE_CHARS = 4000
MAX_CATEGORY_CHARS = 40
MAX_PAGE_CHARS = 300
MAX_REPO_CHARS = 200

# Categories the UI offers. Anything else is stored as "other" rather than
# rejected, so a client/server version skew cannot lose a submission.
KNOWN_CATEGORIES = {"bug", "suggestion", "content", "praise", "other"}


@dataclass(slots=True)
class FeedbackRecord:
    """One stored submission."""

    id: str
    created_at: float
    created_at_iso: str
    category: str
    message: str
    repo: Optional[str] = None
    page: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "created_at_iso": self.created_at_iso,
            "category": self.category,
            "message": self.message,
            "repo": self.repo,
            "page": self.page,
        }


def _clip(value: Optional[str], limit: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


class FeedbackStore:
    """Append-only JSONL store.

    Writes are serialized with a lock and flushed per record: feedback is
    low-volume and losing a submission to a buffered write would be worse than
    the cost of flushing.
    """

    def __init__(self, data_dir: str) -> None:
        self.path = os.path.join(os.path.abspath(data_dir), FEEDBACK_FILENAME)
        self._lock = threading.Lock()

    def add(
        self,
        *,
        message: str,
        category: str = "other",
        repo: Optional[str] = None,
        page: Optional[str] = None,
    ) -> FeedbackRecord:
        """Validate, clip and append one submission.

        Raises:
            ValueError: if *message* is empty after stripping.
        """
        text = _clip(message, MAX_MESSAGE_CHARS)
        if not text:
            raise ValueError("feedback message must not be empty")

        cat = (_clip(category, MAX_CATEGORY_CHARS) or "other").lower()
        if cat not in KNOWN_CATEGORIES:
            cat = "other"

        now = time.time()
        record = FeedbackRecord(
            id=uuid.uuid4().hex[:16],
            created_at=now,
            created_at_iso=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            category=cat,
            message=text,
            repo=_clip(repo, MAX_REPO_CHARS),
            page=_clip(page, MAX_PAGE_CHARS),
        )

        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
                fh.flush()
        logger.info("feedback: stored %s (%s)", record.id, record.category)
        return record

    def count(self) -> int:
        """Number of stored records (0 when nothing has been submitted)."""
        if not os.path.isfile(self.path):
            return 0
        with self._lock:
            with open(self.path, "r", encoding="utf-8") as fh:
                return sum(1 for line in fh if line.strip())
