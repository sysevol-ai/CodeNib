# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""A local stand-in for the GitHub REST API, spoken to over real HTTP."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

REPO = "acme/widget"
LOG = "setup\n--- FAIL: TestParse (0.01s)\n    config_test.go:7: boom\nFAIL\n"


class FakeGitHub:
    """Serves one issue (#7) and one pull request (#9) that closes it."""

    def __init__(self):
        self.requests: list[tuple[str, str | None]] = []
        self.rate_limit_once = False
        self.extra_comments: list[dict] = []
        self.check_conclusion = "failure"
        self.updated: list[int] = []

    def routes(self, path: str, query: dict, base: str):
        user = {"login": "ann", "type": "User"}
        bot = {"login": "ci[bot]", "type": "Bot"}
        prefix = f"/repos/{REPO}"
        if path == f"{prefix}/issues/7":
            return {"number": 7, "title": "Crash on empty config", "state": "open",
                    "user": user, "created_at": "2026-01-01T00:00:00Z",
                    "body": "It crashes in `parse_config`, see src/config.py:1"}  # fmt: skip
        if path == f"{prefix}/issues/9":
            return {"number": 9, "title": "Handle empty config", "state": "open",
                    "user": user, "created_at": "2026-01-03T00:00:00Z",
                    "body": "Fixes #7", "pull_request": {}}  # fmt: skip
        if path == f"{prefix}/pulls/9":
            return {"merged_at": None, "base": {"sha": "base000"},
                    "head": {"sha": "head111"}}  # fmt: skip
        if path == f"{prefix}/issues/7/timeline":
            page = int(query.get("page", ["1"])[0])
            if page == 1:
                link = f'<{base}{path}?per_page=100&page=2>; rel="next"'
                return [
                    {"event": "labeled", "id": 1, "actor": user, "label": {"name": "bug"},
                     "created_at": "2026-01-01T00:01:00Z"},
                    {"event": "commented", "id": 2, "user": bot, "body": "triaged",
                     "created_at": "2026-01-01T00:02:00Z"},
                ], {"Link": link}  # fmt: skip
            return [
                {"event": "commented", "id": 3, "user": {"login": "bo", "type": "User"},
                 "body": "Return an empty dict.", "created_at": "2026-01-02T00:00:00Z"},
                {"event": "cross-referenced", "actor": user,
                 "created_at": "2026-01-03T00:00:01Z",
                 "source": {"issue": {"number": 9, "title": "Handle empty config",
                                      "pull_request": {}, "state": "open"}}},
                *self.extra_comments,
            ]  # fmt: skip
        if path == f"{prefix}/issues/9/timeline":
            return [
                {"event": "committed", "sha": "head111", "message": "guard\n\nbody",
                 "author": {"name": "ann", "date": "2026-01-03T00:10:00Z"}},
                {"event": "reviewed", "id": 40, "user": {"login": "rev", "type": "User"},
                 "state": "CHANGES_REQUESTED", "body": "Please add a test.",
                 "submitted_at": "2026-01-04T00:00:00Z"},
            ]  # fmt: skip
        if path == f"{prefix}/pulls/9/comments":
            reviewer = {"login": "rev", "type": "User"}
            hunk = "@@ -1,2 +1,3 @@\n def parse_config(path):\n+    x = 1"
            return [{"id": 60, "user": reviewer, "path": "src/config.py",
                     "line": 2, "body": "Guard here.", "diff_hunk": hunk,
                     "created_at": "2026-01-04T00:00:01Z"}]  # fmt: skip
        if path == f"{prefix}/pulls/9/commits":
            return [{"sha": "head111",
                     "commit": {"committer": {"date": "2026-01-03T00:10:00Z"}}}]  # fmt: skip
        if path == f"{prefix}/pulls/9/files":
            return [{"filename": "src/config.py", "additions": 3, "deletions": 1,
                     "patch": "@@ -2,1 +2,3 @@ def parse_config\n-a\n+b"}]  # fmt: skip
        if path == f"{prefix}/commits/head111/check-runs":
            return {"check_runs": [
                {"id": 500, "name": "unit", "conclusion": self.check_conclusion,
                 "completed_at": "2026-01-03T01:00:00Z", "app": {"slug": "actions"}},
                {"id": 501, "name": "lint", "conclusion": "success",
                 "completed_at": "2026-01-03T01:00:01Z", "app": {"slug": "actions"}},
            ]}  # fmt: skip
        if path == f"{prefix}/pulls":
            return [{"number": 9, "title": "Handle empty config", "user": user,
                     "updated_at": "2026-01-04T00:00:00Z", "draft": False},
                    {"number": 11, "title": "Bump deps", "user": bot,
                     "updated_at": "2026-01-05T00:00:00Z"}]  # fmt: skip
        if path == f"{prefix}/issues" and "since" in query:
            return [{"number": n} for n in self.updated]
        return None


@pytest.fixture()
def github(monkeypatch):
    fake = FakeGitHub()
    # A second server plays the storage host a log download redirects to. It
    # must never receive the token.
    storage_seen: list[str | None] = []

    class Storage(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            storage_seen.append(self.headers.get("Authorization"))
            body = LOG.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    storage = ThreadingHTTPServer(("127.0.0.1", 0), Storage)

    class Api(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parts = urlsplit(self.path)
            fake.requests.append((parts.path, self.headers.get("Authorization")))
            base = f"http://127.0.0.1:{self.server.server_address[1]}"
            if fake.rate_limit_once:
                fake.rate_limit_once = False
                self.send_response(403)
                self.send_header("Retry-After", "1")
                self.end_headers()
                return
            if parts.path == f"/repos/{REPO}/actions/jobs/500/logs":
                self.send_response(302)
                self.send_header(
                    "Location", f"http://localhost:{storage.server_address[1]}/log"
                )
                self.end_headers()
                return
            if parts.path.endswith("/boom"):
                self.send_response(500)
                self.end_headers()
                return
            found = fake.routes(parts.path, parse_qs(parts.query), base)
            headers = {}
            if isinstance(found, tuple):
                found, headers = found
            if found is None:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(found).encode()
            self.send_response(200)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    api = ThreadingHTTPServer(("127.0.0.1", 0), Api)
    for server in (api, storage):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    fake.url = f"http://127.0.0.1:{api.server_address[1]}"
    fake.storage_seen = storage_seen
    monkeypatch.setenv("GITHUB_API_URL", fake.url)
    monkeypatch.setenv("GH_TOKEN", "test-token")
    yield fake
    api.shutdown()
    storage.shutdown()
