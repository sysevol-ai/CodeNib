# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Opt-in, bounded first-party funnel events written to ordinary service logs."""

import os
from collections import deque
from time import monotonic
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..log_utils import get_logger

router = APIRouter()
logger = get_logger(__name__)
_recent: deque[float] = deque(maxlen=120)


class ExperienceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    event: Literal[
        "page_view",
        "example_view",
        "example_source_open",
        "example_github_open",
        "example_wiki_open",
        "example_share",
        "repository_submit",
        "generation_form_view",
        "generation_start",
        "first_chapter_read",
        "agent_setup_open",
    ]
    surface: Literal["landing", "wiki"]
    visit: str = Field(pattern=r"^[a-f0-9]{32}$", max_length=32)


@router.post("/api/experience-events", status_code=204)
async def experience_event(request: Request):
    if os.environ.get("CODENIB_EXPERIENCE_EVENTS") != "1":
        return Response(status_code=204)
    payload = await request.body()
    if len(payload) > 256:
        return Response(status_code=413)
    try:
        event = ExperienceEvent.model_validate_json(payload)
    except ValidationError:
        # Never echo or log the rejected body, including unknown fields.
        return Response(status_code=422)
    now = monotonic()
    # One event-loop operation: there is no await between admission and append.
    # Retain at most 120 timestamps and admit at most 120 logs/minute/worker.
    if len(_recent) == _recent.maxlen and now - _recent[0] < 60:
        return Response(status_code=429, headers={"Retry-After": "60"})
    _recent.append(now)
    logger.info(
        "experience_event event=%s surface=%s visit=%s",
        event.event,
        event.surface,
        event.visit,
    )
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
