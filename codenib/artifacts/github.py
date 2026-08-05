# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve commit-addressed CodeNib artifacts through the GitHub REST API."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..compiler.snapshot_store import normalize_repo
from ..paths import user_state_dir
from .archive import (
    DEFAULT_MAX_ARCHIVE_FILES,
    DEFAULT_MAX_EXPANDED_BYTES,
    extract_context_artifact_archive,
)
from .runtime import VerifiedContextArtifact, verify_context_artifact

GITHUB_API_VERSION = "2026-03-10"
DEFAULT_GITHUB_TOKEN_ENV = "GH_TOKEN"
DEFAULT_MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024 * 1024
_MAX_API_RESPONSE_BYTES = 16 * 1024 * 1024
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_GITHUB_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_USER_AGENT = "CodeNib-context-artifact"


@dataclass(frozen=True, slots=True)
class GitHubArtifactRecord:
    """GitHub provenance for one resolved workflow artifact."""

    artifact_id: int
    name: str
    repository: str
    head_sha: str
    archive_download_url: str
    archive_digest: str
    size_in_bytes: int
    created_at: str


@dataclass(frozen=True, slots=True)
class GitHubArtifactFetchResult:
    """Verified cache result from a GitHub artifact lookup."""

    artifact: VerifiedContextArtifact
    record: GitHubArtifactRecord | None
    downloaded: bool


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _github_repository(value: str) -> tuple[str, str, str]:
    raw = value.strip().removesuffix(".git")
    if not _GITHUB_REPOSITORY_RE.fullmatch(raw):
        raise ValueError("GitHub repository must use owner/name form")
    normalized = normalize_repo(raw)
    owner, name = normalized.split("/", 1)
    return normalized, owner, name


def _full_commit(value: str) -> str:
    commit = value.strip().lower()
    if not _COMMIT_RE.fullmatch(commit):
        raise ValueError("GitHub artifact resolution requires a full Git SHA")
    return commit


def default_github_artifact_name(repository: str, commit: str) -> str:
    """Return the name emitted by the CodeNib publishing Action."""

    normalized, _owner, _name = _github_repository(repository)
    resolved_commit = _full_commit(commit)
    return f"codenib-context-{normalized.replace('/', '-')}-{resolved_commit[:12]}"


def default_context_artifact_dir(repository: str, commit: str) -> Path:
    """Return the user-owned cache path for one repository commit."""

    _normalized, owner, name = _github_repository(repository)
    return user_state_dir() / "artifacts" / owner / name / _full_commit(commit)


def _validated_api_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.rstrip("/"))
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("GitHub API URL must be an HTTPS origin or path")
    return urllib.parse.urlunsplit(parsed)


def _request_headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": _USER_AGENT,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _github_json(url: str, *, token: str | None) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=_request_headers(token))
    # The API response never needs a redirect. Refusing one prevents a custom
    # or compromised API origin from forwarding the bearer token elsewhere.
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=30) as response:
            payload = response.read(_MAX_API_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            code = exc.code
        finally:
            exc.close()
        raise RuntimeError(f"GitHub API request failed with HTTP {code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("GitHub API request failed") from exc
    if len(payload) > _MAX_API_RESPONSE_BYTES:
        raise RuntimeError("GitHub API response exceeded the safety limit")
    try:
        value = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("GitHub API returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("GitHub API returned an unexpected response")
    return value


def _record(
    value: object,
    *,
    repository: str,
    expected_name: str,
    expected_commit: str,
    api_origin: tuple[str, str],
) -> GitHubArtifactRecord | None:
    if not isinstance(value, Mapping):
        return None
    if value.get("name") != expected_name or value.get("expired") is not False:
        return None
    workflow_run = value.get("workflow_run")
    if not isinstance(workflow_run, Mapping):
        return None
    head_sha = workflow_run.get("head_sha")
    if head_sha != expected_commit:
        return None
    artifact_id = value.get("id")
    size = value.get("size_in_bytes")
    digest = value.get("digest")
    download_url = value.get("archive_download_url")
    created_at = value.get("created_at")
    if (
        not isinstance(artifact_id, int)
        or isinstance(artifact_id, bool)
        or artifact_id <= 0
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or not isinstance(digest, str)
        or not _DIGEST_RE.fullmatch(digest)
        or not isinstance(download_url, str)
        or not isinstance(created_at, str)
    ):
        return None
    parsed_download = urllib.parse.urlsplit(download_url)
    if (parsed_download.scheme, parsed_download.netloc) != api_origin:
        return None
    return GitHubArtifactRecord(
        artifact_id=artifact_id,
        name=expected_name,
        repository=repository,
        head_sha=head_sha,
        archive_download_url=download_url,
        archive_digest=digest,
        size_in_bytes=size,
        created_at=created_at,
    )


def resolve_github_context_artifact(
    repository: str,
    commit: str,
    *,
    artifact_name: str | None = None,
    token: str | None = None,
    api_url: str = "https://api.github.com",
) -> GitHubArtifactRecord:
    """Resolve the newest non-expired artifact for one exact workflow SHA."""

    normalized, owner, name = _github_repository(repository)
    commit = _full_commit(commit)
    artifact_name = artifact_name or default_github_artifact_name(
        normalized,
        commit,
    )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", artifact_name):
        raise ValueError("GitHub artifact name is invalid")
    api_url = _validated_api_url(api_url)
    parsed_api = urllib.parse.urlsplit(api_url)
    api_origin = (parsed_api.scheme, parsed_api.netloc)
    encoded_name = urllib.parse.urlencode(
        {"name": artifact_name, "per_page": 100},
    )
    endpoint = (
        f"{api_url}/repos/{urllib.parse.quote(owner, safe='')}/"
        f"{urllib.parse.quote(name, safe='')}/actions/artifacts?{encoded_name}"
    )
    payload = _github_json(endpoint, token=token)
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        raise RuntimeError("GitHub artifact listing has no artifact array")
    records = [
        record
        for item in artifacts
        if (
            record := _record(
                item,
                repository=normalized,
                expected_name=artifact_name,
                expected_commit=commit,
                api_origin=api_origin,
            )
        )
    ]
    if not records:
        raise FileNotFoundError(
            "no non-expired CodeNib artifact matches "
            f"{normalized}@{commit[:12]} with name {artifact_name!r}"
        )
    return max(records, key=lambda item: item.artifact_id)


def _download_archive(
    url: str,
    output: Path,
    *,
    token: str,
    max_bytes: int,
) -> tuple[int, str]:
    """Download through GitHub's redirect without forwarding its token."""

    request = urllib.request.Request(url, headers=_request_headers(token))
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        response = opener.open(request, timeout=30)
    except urllib.error.HTTPError as exc:
        try:
            if exc.code != 302:
                raise RuntimeError(
                    f"GitHub artifact download failed with HTTP {exc.code}"
                ) from exc
            location = exc.headers.get("Location")
        finally:
            exc.close()
    except urllib.error.URLError as exc:
        raise RuntimeError("GitHub artifact download request failed") from exc
    else:
        response.close()
        raise RuntimeError("GitHub artifact download did not return a redirect")
    if not location:
        raise RuntimeError("GitHub artifact download redirect is missing")
    parsed = urllib.parse.urlsplit(location)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        raise RuntimeError("GitHub artifact download redirect is unsafe")

    redirected = urllib.request.Request(
        location,
        headers={"User-Agent": _USER_AGENT},
    )
    digest = hashlib.sha256()
    size = 0
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        with urllib.request.urlopen(redirected, timeout=60) as response:
            with os.fdopen(os.open(output, flags, 0o600), "wb") as handle:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise RuntimeError(
                            f"GitHub artifact download exceeds {max_bytes} bytes"
                        )
                    digest.update(chunk)
                    handle.write(chunk)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"GitHub artifact object download failed with HTTP {exc.code}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("GitHub artifact object download failed") from exc
    return size, digest.hexdigest()


def fetch_github_context_artifact(
    repository: str,
    commit: str,
    *,
    output_dir: str | Path | None = None,
    artifact_name: str | None = None,
    token: str | None = None,
    token_env: str = DEFAULT_GITHUB_TOKEN_ENV,
    api_url: str = "https://api.github.com",
    force: bool = False,
    max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
    max_files: int = DEFAULT_MAX_ARCHIVE_FILES,
    max_expanded_bytes: int = DEFAULT_MAX_EXPANDED_BYTES,
) -> GitHubArtifactFetchResult:
    """Download, archive-verify, extract, and cache one GitHub artifact."""

    repository, _owner, _name = _github_repository(repository)
    commit = _full_commit(commit)
    if not _ENV_NAME_RE.fullmatch(token_env):
        raise ValueError("GitHub token environment variable name is invalid")
    output_candidate = (
        Path(output_dir).expanduser()
        if output_dir is not None
        else default_context_artifact_dir(repository, commit)
    )
    if output_candidate.is_symlink():
        raise ValueError(
            f"context artifact output must not be a symbolic link: {output_candidate}"
        )
    output = output_candidate.resolve()
    if output.is_dir() and (output / "codenib-context.json").is_file() and not force:
        return GitHubArtifactFetchResult(
            artifact=verify_context_artifact(
                output,
                expected_repository=repository,
                expected_commit=commit,
                max_files=max_files,
                max_bytes=max_expanded_bytes,
            ),
            record=None,
            downloaded=False,
        )
    token = token or os.environ.get(token_env)
    if not token:
        raise ValueError(f"GitHub artifact download requires a token in {token_env}")

    record = resolve_github_context_artifact(
        repository,
        commit,
        artifact_name=artifact_name,
        token=token,
        api_url=api_url,
    )
    if record.size_in_bytes > max_download_bytes:
        raise ValueError(
            "GitHub artifact exceeds the configured download limit: "
            f"{record.size_in_bytes} > {max_download_bytes}"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".zip",
        dir=str(output.parent),
    )
    os.close(descriptor)
    archive = Path(temporary_name)
    try:
        _size, digest = _download_archive(
            record.archive_download_url,
            archive,
            token=token,
            max_bytes=max_download_bytes,
        )
        if f"sha256:{digest}" != record.archive_digest:
            raise ValueError("GitHub artifact archive digest does not match the API")
        artifact = extract_context_artifact_archive(
            archive,
            output,
            expected_repository=repository,
            expected_commit=commit,
            max_files=max_files,
            max_bytes=max_expanded_bytes,
        )
    finally:
        archive.unlink(missing_ok=True)
    return GitHubArtifactFetchResult(
        artifact=artifact,
        record=record,
        downloaded=True,
    )


__all__ = [
    "DEFAULT_GITHUB_TOKEN_ENV",
    "DEFAULT_MAX_DOWNLOAD_BYTES",
    "GITHUB_API_VERSION",
    "GitHubArtifactFetchResult",
    "GitHubArtifactRecord",
    "default_context_artifact_dir",
    "default_github_artifact_name",
    "fetch_github_context_artifact",
    "resolve_github_context_artifact",
]
