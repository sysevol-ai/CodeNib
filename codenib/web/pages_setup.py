# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Set a repository up to publish its Wiki on GitHub Pages.

``codenib pages init`` writes a caller workflow for the reusable Pages
workflow and prints what is left to do in the repository's settings.
``codenib pages badge`` prints a README badge that links to the published
Wiki; the badge image is served by that same site, not by a third party.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

DEFAULT_WIKI_MODEL = "openrouter/deepseek/deepseek-v4.1-flash"
DEFAULT_WIKI_SECRET = "OPENROUTER_API_KEY"
WORKFLOW_PATH = Path(".github") / "workflows" / "codenib-wiki.yml"
BADGE_NAME = "wiki-badge.svg"
_GITHUB_REMOTE = re.compile(
    r"^(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<name>[A-Za-z0-9_.-]{1,100}?)"
    r"(?:\.git)?/?$"
)
_SECRET = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")

# A flat two-part badge, drawn once; it reads "Wiki | CodeNib".
WIKI_BADGE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="104" height="20" '
    'role="img" aria-label="Wiki: CodeNib">'
    "<title>Wiki: CodeNib</title>"
    '<linearGradient id="s" x2="0" y2="100%">'
    '<stop offset="0" stop-color="#bbb" stop-opacity=".1"/>'
    '<stop offset="1" stop-opacity=".1"/></linearGradient>'
    '<clipPath id="r"><rect width="104" height="20" rx="3" fill="#fff"/></clipPath>'
    '<g clip-path="url(#r)"><rect width="37" height="20" fill="#555"/>'
    '<rect x="37" width="67" height="20" fill="#2f6feb"/>'
    '<rect width="104" height="20" fill="url(#s)"/></g>'
    '<g fill="#fff" text-anchor="middle" '
    'font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">'
    '<text x="18.5" y="14">Wiki</text><text x="69.5" y="14">CodeNib</text></g>'
    "</svg>\n"
)


def _git(repo: Path, *args: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def github_repository(repo: Path) -> Optional[str]:
    """``owner/name`` of the checkout's GitHub origin, if it has one."""

    match = _GITHUB_REMOTE.match(_git(repo, "remote", "get-url", "origin") or "")
    return f"{match['owner']}/{match['name']}" if match else None


def default_branch(repo: Path) -> str:
    """The origin's default branch, else the checked-out one, else ``main``."""

    head = _git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if head and head.startswith("origin/"):
        return head.removeprefix("origin/")
    current = _git(repo, "branch", "--show-current")
    return current or "main"


def pages_url(repository: str) -> str:
    """Where GitHub Pages serves ``owner/name`` without a custom domain."""

    owner, name = repository.split("/", 1)
    host = f"{owner.lower()}.github.io"
    if name.lower() == host:
        return f"https://{host}/"
    return f"https://{host}/{name}/"


def badge_markdown(site_url: str) -> str:
    root = site_url if site_url.endswith("/") else site_url + "/"
    return f"[![Wiki: CodeNib]({root}{BADGE_NAME})]({root})"


def workflow_text(
    *,
    ref: str,
    branch: str,
    model: str = DEFAULT_WIKI_MODEL,
    secret: str = DEFAULT_WIKI_SECRET,
) -> str:
    """A caller workflow for the reusable CodeNib Pages workflow."""

    if not _REF.fullmatch(ref) or ".." in ref:
        raise ValueError(f"unsupported workflow ref: {ref!r}")
    if not _REF.fullmatch(branch) or ".." in branch:
        raise ValueError(f"unsupported branch name: {branch!r}")
    if not _MODEL.fullmatch(model):
        raise ValueError(f"unsupported model name: {model!r}")
    if not _SECRET.fullmatch(secret) or secret.upper().startswith("GITHUB_"):
        raise ValueError(f"unsupported secret name: {secret!r}")
    lines = [
        "# Publishes this repository's CodeNib Wiki to GitHub Pages.",
        "# Written by `codenib pages init`; see https://docs.codenib.ai/github_pages/",
        "name: CodeNib Wiki",
        "",
        "on:",
        "  push:",
        f"    branches: [{branch}]",
        "  workflow_dispatch:",
        "    inputs:",
        "      full-rebuild:",
        "        description: Rewrite every page instead of keeping unchanged ones",
        "        type: boolean",
        "        default: false",
        "",
        "permissions:",
        "  contents: read",
        "  pages: write",
        "  id-token: write",
        "",
        "jobs:",
        "  publish:",
        f"    uses: sysevol-ai/CodeNib/.github/workflows/codenib-pages.yml@{ref}",
        "    with:",
        "      preset: fast",
        "      wiki: story",
        f"      model: {model}",
        "      full-rebuild: ${{ inputs.full-rebuild || false }}",
        "    secrets:",
        f"      wiki_api_key: ${{{{ secrets.{secret} }}}}",
    ]
    return "\n".join(lines) + "\n"


__all__ = [
    "BADGE_NAME",
    "DEFAULT_WIKI_MODEL",
    "DEFAULT_WIKI_SECRET",
    "WIKI_BADGE_SVG",
    "WORKFLOW_PATH",
    "badge_markdown",
    "default_branch",
    "github_repository",
    "pages_url",
    "workflow_text",
]
