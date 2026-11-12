# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Read one GitHub issue or pull request as a list of thread events.

Only GET requests are made. Thread text is returned as data; nothing in it is
interpreted.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .events import FAILED_CONCLUSIONS, ThreadEvent, ThreadHeader, event_address

_API = "https://api.github.com"
_USER_AGENT = "codenib-threads"
_NOISE_EVENTS = frozenset({"subscribed", "unsubscribed", "mentioned", "line-commented"})
_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')
_MAX_LOG_BYTES = 2_000_000
_MAX_PAGES = 30
_RATE_LIMIT_RETRIES = 3
_MAX_RATE_LIMIT_WAIT = 300

DEFAULT_MAX_COMMITS = 8
DEFAULT_MAX_FAILED_LOGS = 6


def resolve_token() -> str | None:
    """Token from the environment, else from an installed ``gh``."""

    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    try:
        proc = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() or None


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect without forwarding the bearer token to the new host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != (
            urllib.parse.urlsplit(req.full_url).netloc
        ):
            new.remove_header("Authorization")
            new.unredirected_hdrs.pop("Authorization", None)
        return new


class GitHubClient:
    """Minimal read-only REST client. Missing resources return ``None``."""

    def __init__(self, token: str | None = None, api_url: str = _API) -> None:
        self._token = token
        self._api = api_url.rstrip("/")
        self._opener = urllib.request.build_opener(_DropAuthOnRedirect())
        self._sleep = time.sleep
        self.requests = 0

    def _open(self, url: str, accept: str):
        headers = {
            "Accept": accept,
            "User-Agent": _USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        request = urllib.request.Request(url, headers=headers)
        if self._token:
            # Unredirected, so it is never replayed to a storage host.
            request.add_unredirected_header("Authorization", f"Bearer {self._token}")
        # GitHub answers a burst with 403 or 429 and a Retry-After header. Wait
        # as told, a bounded number of times, instead of failing the sync.
        for attempt in range(_RATE_LIMIT_RETRIES + 1):
            self.requests += 1
            try:
                return self._opener.open(request, timeout=60)
            except urllib.error.HTTPError as exc:
                limited = exc.code == 429 or (
                    exc.code == 403
                    and (
                        exc.headers.get("Retry-After")
                        or exc.headers.get("X-RateLimit-Remaining") == "0"
                    )
                )
                if not limited or attempt == _RATE_LIMIT_RETRIES:
                    raise
                wait = int(exc.headers.get("Retry-After") or 0) or 60 * (attempt + 1)
                self._sleep(min(wait, _MAX_RATE_LIMIT_WAIT))
        raise AssertionError("unreachable")

    def _url(self, path: str) -> str:
        return path if path.startswith("https://") else f"{self._api}/{path}"

    def get(self, path: str) -> Any:
        try:
            with self._open(self._url(path), "application/vnd.github+json") as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410, 422, 451):
                return None
            raise RuntimeError(f"GitHub request failed: HTTP {exc.code}") from exc

    def pages(
        self, path: str, key: str | None = None, max_pages: int = _MAX_PAGES
    ) -> list:
        sep = "&" if "?" in path else "?"
        url: str | None = self._url(f"{path}{sep}per_page=100")
        out: list = []
        for _ in range(max_pages):
            if not url:
                break
            try:
                with self._open(url, "application/vnd.github+json") as resp:
                    page = json.loads(resp.read())
                    link = resp.headers.get("Link", "")
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 410, 422, 451):
                    break
                raise RuntimeError(f"GitHub request failed: HTTP {exc.code}") from exc
            out.extend(page.get(key, []) if key else page)
            match = _NEXT_RE.search(link)
            url = match.group(1) if match else None
        return out

    def text(self, path: str) -> str | None:
        try:
            with self._open(self._url(path), "application/vnd.github+json") as resp:
                return resp.read(_MAX_LOG_BYTES).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404, 410):
                return None
            raise RuntimeError(f"GitHub request failed: HTTP {exc.code}") from exc


def _login(user: dict | None) -> str:
    user = user or {}
    return user.get("login") or user.get("name") or "?"


# Automation that GitHub reports as an ordinary user account.
_KNOWN_BOT_LOGINS = frozenset(
    {
        "claassistant",
        "codecov-commenter",
        "codecov-io",
        "coveralls",
        "googlebot",
        "k8s-ci-robot",
        "netlify",
        "sonarcloud",
        "vercel",
    }
)


def _is_bot(user: dict | None) -> bool:
    user = user or {}
    login = (user.get("login") or "").lower()
    return (
        user.get("type") == "Bot"
        or login.endswith("[bot]")
        or login.endswith("-bot")
        or login in _KNOWN_BOT_LOGINS
    )


def _closing_refs(body: str) -> list[int]:
    pattern = r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+#(\d+)"
    return [int(n) for n in re.findall(pattern, body or "")]


def _timeline(
    repo: str, number: int, items: list, links: dict[str, str]
) -> list[ThreadEvent]:
    events = []
    for index, item in enumerate(items):
        name = item.get("event") or "?"
        if name in _NOISE_EVENTS:
            continue
        ident = item.get("id") or item.get("node_id") or item.get("sha") or index
        if name == "commented":
            events.append(
                ThreadEvent(
                    event_address(repo, number, "comment", ident),
                    number,
                    "comment",
                    _login(item.get("user")),
                    item.get("created_at") or "",
                    _is_bot(item.get("user")),
                    item.get("body") or "",
                )
            )
        elif name == "reviewed":
            events.append(
                ThreadEvent(
                    event_address(repo, number, "review", ident),
                    number,
                    "review",
                    _login(item.get("user")),
                    item.get("submitted_at") or "",
                    _is_bot(item.get("user")),
                    item.get("body") or "",
                    {"state": (item.get("state") or "").lower()},
                )
            )
        elif name == "committed":
            author = item.get("author") or {}
            events.append(
                ThreadEvent(
                    event_address(repo, number, "commit", item.get("sha")),
                    number,
                    "commit",
                    author.get("name") or "?",
                    author.get("date") or "",
                    False,
                    "",
                    {
                        "sha": item.get("sha"),
                        "subject": (item.get("message") or "").split("\n")[0],
                    },
                )
            )
        elif name == "cross-referenced":
            source = (item.get("source") or {}).get("issue") or {}
            source_repo = (source.get("repository") or {}).get("full_name") or repo
            ref = source.get("number")
            if ref is None:
                continue
            same_repo = source_repo.lower() == repo.lower()
            if same_repo:
                links.setdefault(str(ref), "mentions")
            events.append(
                ThreadEvent(
                    event_address(repo, number, "cross_reference", f"{ref}-{index}"),
                    number,
                    "cross_reference",
                    _login(item.get("actor")),
                    item.get("created_at") or "",
                    _is_bot(item.get("actor")),
                    "",
                    {
                        "from": ref if same_repo else f"{source_repo}#{ref}",
                        "title": source.get("title") or "",
                        "is_pr": "pull_request" in source,
                        "state": source.get("state") or "",
                    },
                )
            )
        else:
            meta: dict[str, Any] = {}
            if item.get("label"):
                meta["label"] = item["label"].get("name")
            if item.get("assignee"):
                meta["assignee"] = item["assignee"].get("login")
            if item.get("rename"):
                meta["to"] = item["rename"].get("to")
            if item.get("commit_id"):
                meta["sha"] = item["commit_id"]
            events.append(
                ThreadEvent(
                    event_address(repo, number, name, ident),
                    number,
                    name,
                    _login(item.get("actor")),
                    item.get("created_at") or "",
                    _is_bot(item.get("actor")),
                    "",
                    meta,
                )
            )
    return events


def fetch_thread(
    client: GitHubClient,
    repo: str,
    number: int,
    *,
    max_commits: int = DEFAULT_MAX_COMMITS,
    max_failed_logs: int = DEFAULT_MAX_FAILED_LOGS,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[ThreadHeader, list[ThreadEvent]] | None:
    """Fetch one issue or pull request. Returns ``None`` if it does not exist."""

    issue = client.get(f"repos/{repo}/issues/{number}")
    if not issue:
        return None
    is_pr = "pull_request" in issue
    links: dict[str, str] = {}
    pull = client.get(f"repos/{repo}/pulls/{number}") if is_pr else None
    state = issue.get("state") or "open"
    if pull and pull.get("merged_at"):
        state = "merged"
    header = ThreadHeader(
        repo=repo,
        number=number,
        is_pr=is_pr,
        title=issue.get("title") or "",
        state=state,
        author=_login(issue.get("user")),
        created_at=issue.get("created_at") or "",
        closed_at=issue.get("closed_at") or "",
        base_sha=((pull or {}).get("base") or {}).get("sha") or "",
        head_sha=((pull or {}).get("head") or {}).get("sha") or "",
        links=links,
    )
    events = [
        ThreadEvent(
            event_address(repo, number, "opened", number),
            number,
            "opened",
            header.author,
            header.created_at,
            _is_bot(issue.get("user")),
            issue.get("body") or "",
            {"title": header.title, "is_pr": is_pr},
        )
    ]
    if is_pr:
        for ref in _closing_refs(issue.get("body") or ""):
            links[str(ref)] = "closes"
    events += _timeline(
        repo, number, client.pages(f"repos/{repo}/issues/{number}/timeline"), links
    )
    if is_pr:
        events += _pull_events(
            client, repo, number, max_commits, max_failed_logs, on_progress
        )
    events = [event for event in events if event.at]
    events.sort(key=lambda event: event.at)
    return header, events


def _pull_events(
    client: GitHubClient,
    repo: str,
    number: int,
    max_commits: int,
    max_failed_logs: int,
    on_progress: Callable[[str], None] | None,
) -> list[ThreadEvent]:
    events = []
    for comment in client.pages(f"repos/{repo}/pulls/{number}/comments"):
        events.append(
            ThreadEvent(
                event_address(repo, number, "review_comment", comment.get("id")),
                number,
                "review_comment",
                _login(comment.get("user")),
                comment.get("created_at") or "",
                _is_bot(comment.get("user")),
                comment.get("body") or "",
                {
                    "path": comment.get("path") or "",
                    "line": comment.get("line") or comment.get("original_line"),
                    "hunk": comment.get("diff_hunk") or "",
                },
            )
        )
    commits = client.pages(f"repos/{repo}/pulls/{number}/commits")
    shas = [c["sha"] for c in commits]
    files = client.pages(f"repos/{repo}/pulls/{number}/files")
    if files and commits:
        last = (commits[-1].get("commit") or {}).get("committer") or {}
        events.append(
            ThreadEvent(
                event_address(repo, number, "diff", shas[-1]),
                number,
                "diff",
                "git",
                last.get("date") or "",
                True,
                "",
                {
                    "sha": shas[-1],
                    "files": [f.get("filename") for f in files[:100]],
                    "additions": sum(f.get("additions", 0) for f in files),
                    "deletions": sum(f.get("deletions", 0) for f in files),
                },
            )
        )
    logs_left = max_failed_logs
    for sha in reversed(shas[-max_commits:]):
        runs = client.pages(f"repos/{repo}/commits/{sha}/check-runs", key="check_runs")
        for run in runs:
            conclusion = run.get("conclusion") or run.get("status") or "?"
            meta: dict[str, Any] = {
                "name": run.get("name") or "",
                "conclusion": conclusion,
                "sha": sha,
            }
            if conclusion in FAILED_CONCLUSIONS:
                log = None
                if logs_left > 0:
                    logs_left -= 1
                    if on_progress:
                        on_progress(f"log for failed check {run.get('name')}")
                    log = client.text(f"repos/{repo}/actions/jobs/{run['id']}/logs")
                meta["log"] = log or ""
                meta["log_missing"] = log is None
            events.append(
                ThreadEvent(
                    event_address(repo, number, "check", run.get("id")),
                    number,
                    "check",
                    (run.get("app") or {}).get("slug") or "ci",
                    run.get("completed_at") or run.get("started_at") or "",
                    True,
                    (run.get("output") or {}).get("summary") or "",
                    meta,
                )
            )
    return events


__all__ = ["GitHubClient", "fetch_thread", "resolve_token"]
