// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0
// Requests source excerpts: Requests contributors, Apache-2.0.
// Captured by scripts/build_first_visit_example.py; do not hand-edit.
export default {
  "repository": "psf/requests",
  "commit": "f361ead047be5cb873174218582f7d8b9fcd9f49",
  "page": "redirects",
  "graph_path": "/api/repos/psf__requests/wiki/redirects/graph",
  "license": "Apache-2.0",
  "license_url": "https://github.com/psf/requests/blob/f361ead047be5cb873174218582f7d8b9fcd9f49/LICENSE",
  "nodes": [
    {
      "id": "n7",
      "symbol": "Session.send()",
      "title": "Send the request",
      "file": "src/requests/sessions.py",
      "definition_line": 752,
      "start_line": 800,
      "content": "\n        # Resolve redirects if allowed.\n        if allow_redirects:\n            # Redirect resolving generator.\n            gen = self.resolve_redirects(r, request, **kwargs)\n            history = [resp for resp in gen]\n        else:\n            history = []\n\n",
      "sha256": "c9c7a447bb5f1e044d7a7e09b1b2d524e3537e77ca1a73fd6246730ca7eaea6b"
    },
    {
      "id": "n0",
      "symbol": "SessionRedirectMixin.resolve_redirects()",
      "title": "Follow the redirect",
      "file": "src/requests/sessions.py",
      "definition_line": 186,
      "start_line": 270,
      "content": "\n            # Rebuild auth and proxy information.\n            proxies = self.rebuild_proxies(prepared_request, proxies)\n            self.rebuild_auth(prepared_request, resp)\n\n            # A failed tell() sets `_body_position` to `object()`. This non-None\n",
      "sha256": "e786da6f80064f1bdf455f82ccfd67ed7e9dd61daee5151a972ce73d9c5e86ec"
    },
    {
      "id": "n1",
      "symbol": "SessionRedirectMixin.rebuild_auth()",
      "title": "Rebuild credentials",
      "file": "src/requests/sessions.py",
      "definition_line": 309,
      "start_line": 320,
      "content": "        headers = prepared_request.headers\n        original_url = original_request.url\n        url = prepared_request.url\n\n        if \"Authorization\" in headers and self.should_strip_auth(original_url, url):\n            # If we get redirected to a new host, we should strip out any\n            # authentication headers.\n            del headers[\"Authorization\"]\n\n        # .netrc might have more auth for us on our new host.\n        new_auth = get_netrc_auth(url) if self.trust_env else None\n        if new_auth is not None:\n            prepared_request.prepare_auth(new_auth)\n",
      "sha256": "aa54a34b5857e0090804ce234d656d6b256e6f636856b80abccd860a06552872"
    },
    {
      "id": "n3",
      "symbol": "SessionRedirectMixin.should_strip_auth()",
      "title": "Check the destination",
      "file": "src/requests/sessions.py",
      "definition_line": 154,
      "start_line": 154,
      "content": "    def should_strip_auth(self, old_url: str, new_url: str) -> bool:\n        \"\"\"Decide whether Authorization header should be removed when redirecting\"\"\"\n        old_parsed = urlparse(old_url)\n        new_parsed = urlparse(new_url)\n        if old_parsed.hostname != new_parsed.hostname:\n            return True\n        # Special case: allow http -> https redirect when using the standard\n",
      "sha256": "a7b776379a0f8049a481c5484913328032e5e5f30c026c5fa27906d5d946878f"
    }
  ],
  "edges": [
    {
      "source": "n7",
      "target": "n0",
      "anchor": {
        "file": "src/requests/sessions.py",
        "line": 804
      }
    },
    {
      "source": "n0",
      "target": "n1",
      "anchor": {
        "file": "src/requests/sessions.py",
        "line": 273
      }
    },
    {
      "source": "n1",
      "target": "n3",
      "anchor": {
        "file": "src/requests/sessions.py",
        "line": 324
      }
    }
  ]
};
