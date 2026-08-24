# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""OpenAI-compatible VLM extraction for repository media artifacts."""

from __future__ import annotations

import base64
import json
import math
import os
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .media_facts import (
    build_visual_fact_extraction_prompt,
    normalize_visual_fact_pack,
)

_MAX_MODEL_LENGTH = 256
_MAX_PROVIDER_LENGTH = 128
_MAX_URL_LENGTH = 4096
_MAX_TIMEOUT_SECONDS = 600.0
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_IMAGE_BYTES = 16 * 1024 * 1024
_SUPPORTED_MIME_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/svg+xml",
        "image/webp",
    }
)


class OpenAICompatibleVisualFactExtractor:
    """Extract structured visual facts through an OpenAI-compatible chat API."""

    def __init__(
        self,
        *,
        model: str,
        api_base: str,
        api_key: str | None = None,
        timeout: float = 120.0,
        urlopen: Callable[..., Any] | None = None,
        provider: str = "openai-compatible",
    ) -> None:
        self.model = str(model or "").strip()
        self.api_base = str(api_base or "").strip()
        self.api_key = _validated_api_key(api_key)
        self.timeout = _validated_timeout(timeout)
        self.provider = str(provider or "").strip()
        self._urlopen = urlopen or urllib.request.urlopen
        if not self.model:
            raise ValueError("visual fact model is required")
        if len(self.model) > _MAX_MODEL_LENGTH:
            raise ValueError("visual fact model is too long")
        if not self.provider or len(self.provider) > _MAX_PROVIDER_LENGTH:
            raise ValueError("visual fact provider is invalid")
        self._endpoint = _chat_completions_endpoint(self.api_base)

    @property
    def endpoint(self) -> str:
        return self._endpoint

    def extract(
        self,
        artifact: Mapping[str, Any],
        *,
        repo_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Extract one canonical visual fact pack for *artifact*."""

        prompt = build_visual_fact_extraction_prompt(artifact)
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        if repo_path is not None:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": _artifact_data_url(repo_path, artifact),
                    },
                }
            )
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You extract structured repository visual facts. "
                        "Return JSON only."
                    ),
                },
                {"role": "user", "content": content},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        response = self._post_json(payload)
        extracted = _response_content_json(response)
        extracted.setdefault("artifact_path", str(artifact.get("path") or ""))
        extracted.setdefault("artifact_sha256", str(artifact.get("sha256") or ""))
        extracted.setdefault("role_hint", str(artifact.get("role_hint") or ""))
        extracted["extractor"] = self.provider
        metadata = dict(extracted.get("metadata") or {})
        metadata.update({"model": self.model, "provider": self.provider})
        extracted["metadata"] = metadata
        return normalize_visual_fact_pack(extracted)

    def __call__(self, artifact: Mapping[str, Any]) -> dict[str, Any]:
        return self.extract(artifact)

    def _post_json(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with self._urlopen(request, timeout=self.timeout) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ValueError("visual fact response exceeds the byte limit")
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("visual fact response must be a JSON object")
        return data


def _response_content_json(response: Mapping[str, Any]) -> dict[str, Any]:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("visual fact response must contain choices[0]")
    first = choices[0]
    if not isinstance(first, Mapping):
        raise ValueError("visual fact response choice must be an object")
    message = first.get("message")
    if not isinstance(message, Mapping):
        raise ValueError("visual fact response choice must contain message")
    content = message.get("content")
    if not isinstance(content, str):
        raise ValueError("visual fact response message content must be a string")
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("visual fact response content must decode to a JSON object")
    return parsed


def _artifact_data_url(repo_path: str | Path, artifact: Mapping[str, Any]) -> str:
    root = Path(repo_path).expanduser().resolve()
    relative = _safe_relative_path(artifact.get("path"))
    path = (root / relative).resolve()
    if not (path == root or root in path.parents):
        raise ValueError("visual artifact path is outside the repository")
    if path.is_symlink() or not path.is_file():
        raise ValueError("visual artifact must be a regular file")
    mime_type = str(artifact.get("mime_type") or "").strip()
    if mime_type not in _SUPPORTED_MIME_TYPES:
        raise ValueError("visual artifact MIME type is unsupported")
    size = path.stat().st_size
    if size < 0 or size > _MAX_IMAGE_BYTES:
        raise ValueError("visual artifact exceeds the byte limit")
    data = _read_bounded_file(path, max_bytes=_MAX_IMAGE_BYTES)
    return f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"


def _safe_relative_path(value: Any) -> Path:
    text = str(value or "").strip()
    if not text:
        raise ValueError("visual artifact path is required")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != text:
        raise ValueError("visual artifact path must be repository-relative")
    return Path(*path.parts)


def _read_bounded_file(path: Path, *, max_bytes: int) -> bytes:
    chunks = []
    consumed = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            consumed += len(chunk)
            if consumed > max_bytes:
                raise ValueError("visual artifact exceeds the byte limit")
            chunks.append(chunk)
    return b"".join(chunks)


def _validated_timeout(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("visual fact timeout must be a positive number")
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("visual fact timeout must be a positive number") from exc
    if not math.isfinite(timeout) or not 0 < timeout <= _MAX_TIMEOUT_SECONDS:
        raise ValueError(
            f"visual fact timeout must be between 0 and {_MAX_TIMEOUT_SECONDS:g} seconds"
        )
    return timeout


def _validated_api_key(value: Any) -> str | None:
    if value is None:
        return None
    api_key = str(value)
    if len(api_key) > 8192 or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in api_key
    ):
        raise ValueError("visual fact API key is invalid")
    return api_key


def _validated_http_url(value: str, *, label: str) -> str:
    url = str(value or "").strip()
    if not url or len(url) > _MAX_URL_LENGTH:
        raise ValueError(f"{label} is invalid")
    if any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in url
    ):
        raise ValueError(f"{label} is invalid")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise ValueError(f"{label} must be an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{label} must not contain credentials")
    return url


def _chat_completions_endpoint(api_base: str) -> str:
    base = _validated_http_url(api_base, label="visual fact api_base")
    parsed = urlsplit(base)
    path = parsed.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path = f"{path}/chat/completions"
    return parsed._replace(path=path, fragment="").geturl()


__all__ = ["OpenAICompatibleVisualFactExtractor"]
