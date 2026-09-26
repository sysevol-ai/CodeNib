# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""CPU-only candidates for browser-owned OpenRouter requests on public source.

This separate application has no operator Ask, Wiki generation, key exchange,
repository submission or index-building routes. Operators explicitly publish a
small list of source fingerprints already verified for their static Wiki.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Callable
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..agent.decision_rerank import RELEVANCE_CRITERIA
from ..agent.runtime.grep_jev import (
    JEV_MODEL,
    MAX_CONTENT_CHARS,
    PLANNER_MODEL,
    PLANNER_SCHEMA,
    PLANNER_SYSTEM,
    GrepJevError,
    GrepPlan,
    collect_grep_candidates,
    grep_planning_context,
)
from ..compiler.manifest_source import resolve_compiler_source_selection
from ..paths import repo_index_dir
from ..source_fingerprint import capture_repository_source
from .ports import argparse_tcp_port
from .request_limits import RequestBodyLimitMiddleware

Fingerprint = Annotated[str, Field(pattern=r"^sha256-v2:[0-9a-f]{64}$")]


class PublicTrialRepository(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$")
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    root: str = Field(min_length=1, max_length=4096)
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_fingerprint: Fingerprint


class PublicTrialConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    origins: list[str] = Field(min_length=1, max_length=10)
    hosts: list[str] = Field(min_length=1, max_length=10)
    repositories: list[PublicTrialRepository] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_publication(self):
        for origin in self.origins:
            parsed = urlsplit(origin)
            local = parsed.hostname in {"localhost", "127.0.0.1"}
            if (
                not parsed.hostname
                or ":" in parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
                or (
                    parsed.scheme != "https" and not (local and parsed.scheme == "http")
                )
            ):
                raise ValueError(
                    "Trial origins require an HTTPS hostname or localhost/127.0.0.1; "
                    "IPv6 literals are not supported by the browser CSP"
                )
        if any(
            not host or len(host) > 253 or any(c in host for c in "*/\\@:#? ")
            for host in self.hosts
        ):
            raise ValueError("Trial hosts must be explicit hostnames without wildcards")
        if len({repo.id for repo in self.repositories}) != len(self.repositories):
            raise ValueError("Trial repository IDs must be unique")
        if any(
            repo.id in {".", ".."}
            or any(part in {".", ".."} for part in repo.repository.split("/"))
            for repo in self.repositories
        ):
            raise ValueError("Trial repository identity must name a public repository")
        if any(not Path(repo.root).is_absolute() for repo in self.repositories):
            raise ValueError("Trial repository roots must be absolute")
        return self


class CandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_fingerprint: Fingerprint
    plan: GrepPlan


class _Admission:
    """One process: at most two source workers, six calls/IP and 60 calls/minute.

    Admission linearizes under ``lock``. The synchronous request worker owns
    its slot through source/temp-tree cleanup, even when its HTTP client goes
    away. The lock is released before filesystem authority is acquired; no
    state survives process exit and no recovery owner is needed. Run one worker
    behind the public proxy; forwarded IP headers are untrusted unless the
    launcher explicitly admits the proxy's address.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.started = clock()
        self.clients: dict[str, int] = {}
        self.total = 0
        self.active = 0

    @contextmanager
    def enter(self, client: str):
        with self.lock:
            now = self.clock()
            if now - self.started >= 60:
                self.started = now
                self.clients.clear()
                self.total = 0
            if self.active >= 2 or self.total >= 60 or self.clients.get(client, 0) >= 6:
                raise HTTPException(
                    429,
                    "Public trial is busy; try later.",
                    headers={"Retry-After": "60"},
                )
            self.active += 1
            self.total += 1
            self.clients[client] = self.clients.get(client, 0) + 1
        try:
            yield
        finally:
            with self.lock:
                self.active -= 1


def _source_response(repo, plan, check):
    root = Path(repo.root)
    selection = resolve_compiler_source_selection(repo_index_dir(root))
    with capture_repository_source(
        root, selection=selection, check_cancelled=check
    ) as source:
        identity = source.authenticated_identity_snapshot(check_cancelled=check)
        if identity.fingerprint != repo.source_fingerprint:
            raise HTTPException(
                409, "Published source changed; refresh the public corpus."
            )
        if (
            identity.file_count > 5000
            or sum(r.size for r in identity.file_records) > 32 * 1024**2
        ):
            raise HTTPException(
                413, "Repository exceeds the public trial source limit."
            )
        result = {
            "repository": repo.repository,
            "repo_id": repo.id,
            "commit": repo.commit,
            "source_fingerprint": identity.fingerprint,
        }
        if plan is None:
            result.update(grep_planning_context(source, check_cancelled=check))
            result["protocol"] = {
                "planner_model": PLANNER_MODEL,
                "reranker_model": JEV_MODEL,
                "planner_system": PLANNER_SYSTEM,
                "planner_schema": PLANNER_SCHEMA,
                "relevance_criteria": RELEVANCE_CRITERIA,
            }
        else:
            collected = collect_grep_candidates(source, plan, check_cancelled=check)
            candidates = []
            file_lines = {}
            with source.read_session(check_cancelled=check):
                for index, node in enumerate(collected.nodes):
                    check()
                    if node.file not in file_lines:
                        raw = source.read_bytes(node.file, max_bytes=10 * 1024**2)
                        file_lines[node.file] = raw.decode(
                            "utf-8", errors="replace"
                        ).splitlines()
                    snippet = "\n".join(
                        file_lines[node.file][node.start_line : node.end_line + 1]
                    )[:MAX_CONTENT_CHARS]
                    if not snippet:
                        raise GrepJevError("Candidate has no verified source")
                    start = node.start_line + 1
                    end = start + len(snippet.splitlines()) - 1
                    candidates.append(
                        {
                            "id": f"node_{index}",
                            "name": node.node_name,
                            "file": node.file,
                            "content": node.content,
                            "source": snippet,
                            "start_line": start,
                            "end_line": end,
                            "url": (
                                f"https://github.com/{repo.repository}/blob/"
                                f"{repo.commit}/{quote(node.file, safe='/')}"
                                f"#L{start}-L{end}"
                            ),
                        }
                    )
            result["candidates"] = candidates
            result["actions"] = collected.actions
            result["source_file_count"] = collected.source_file_count
            result["skipped_files"] = collected.skipped_files
        source.authenticated_identity_snapshot(check_cancelled=check)
        content = json.dumps(result, ensure_ascii=True, allow_nan=False).encode()
        if len(content) > 3 * 1024**2:
            raise GrepJevError("Candidate response exceeds the public trial limit")
        return Response(
            content,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )


def create_public_trial_app(config: PublicTrialConfig) -> FastAPI:
    """Expose only explicitly published source; never load an operator model/key."""
    app = FastAPI(
        title="CodeNib public source trial",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=16 * 1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=config.hosts, www_redirect=False
    )
    repositories = {repo.id: repo for repo in config.repositories}
    admission = _Admission()
    app.state.admission = admission

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, _error):
        return JSONResponse(
            {"detail": "Invalid public trial request."}, status_code=422
        )

    def execute(repo_id: str, request: Request, payload: CandidateRequest | None):
        if request.headers.get("authorization") or request.headers.get("cookie"):
            raise HTTPException(400, "Do not send credentials to the source service.")
        origin = request.headers.get("origin")
        if origin is not None and origin not in config.origins:
            raise HTTPException(403, "Origin is not enabled for the public trial.")
        repo = repositories.get(repo_id)
        if repo is None:
            raise HTTPException(404, "Repository is not in the public trial.")
        if (
            payload is not None
            and payload.source_fingerprint != repo.source_fingerprint
        ):
            raise HTTPException(
                409, "Published source changed; reconnect to the repository."
            )
        with admission.enter(request.client.host if request.client else "unknown"):
            deadline = time.monotonic() + 30

            def check():
                if time.monotonic() >= deadline:
                    raise GrepJevError("Public source request timed out")

            try:
                return _source_response(repo, payload.plan if payload else None, check)
            except HTTPException:
                raise
            except (OSError, RuntimeError, ValueError):
                # Source paths, subprocess/provider bodies and rejected input
                # never enter public errors or access logs.
                raise HTTPException(
                    503, "Public source is unavailable; no model fallback was started."
                ) from None

    @app.get("/trial/repos/{repo_id}")
    def planning_context(repo_id: str, request: Request):
        return execute(repo_id, request, None)

    @app.post("/trial/repos/{repo_id}/candidates")
    def candidates(repo_id: str, payload: CandidateRequest, request: Request):
        return execute(repo_id, request, payload)

    return app


def _proxy_address(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        raise argparse.ArgumentTypeError("use one explicit proxy IP address") from None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=argparse_tcp_port, default=8001)
    parser.add_argument(
        "--trusted-proxy", type=_proxy_address, action="append", default=[]
    )
    args = parser.parse_args()
    config = PublicTrialConfig.model_validate_json(args.config.read_bytes())
    import uvicorn

    uvicorn.run(
        create_public_trial_app(config),
        host=args.host,
        port=args.port,
        proxy_headers=bool(args.trusted_proxy),
        forwarded_allow_ips=",".join(args.trusted_proxy),
        access_log=False,
    )


if __name__ == "__main__":
    main()
