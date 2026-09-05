# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Provider-neutral materialization for planned wiki media slots."""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import math
import os
import re
import stat
import tempfile
import threading
import urllib.request
import weakref
from dataclasses import asdict, dataclass, field
from itertools import islice
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .media_evidence import MAX_MEDIA_EVIDENCE_PACK_BYTES, normalize_media_evidence_pack

_GENERATABLE_IMAGE_KINDS = frozenset({"diagram", "image", "storyboard"})
_MAX_MEDIA_SLOTS = 12
_MAX_PROMPT_BYTES = 32 * 1024
_MAX_IMAGE_RESPONSE_BYTES = 32 * 1024 * 1024
_MAX_IMAGE_BYTES = 16 * 1024 * 1024
_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_MODEL_LENGTH = 256
_MAX_PROVIDER_LENGTH = 128
_MAX_URL_LENGTH = 4096
_MAX_TIMEOUT_SECONDS = 600.0
_MAX_IMAGE_DIMENSION = 4096
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"
_GEMINI_INTERACTIONS_API_BASE = (
    "https://generativelanguage.googleapis.com/v1beta/interactions"
)
_GEMINI_PROVIDERS = frozenset({"gemini", "gemini-interactions", "google-gemini"})
_GEMINI_ASPECT_RATIOS = frozenset(
    {
        "1:1",
        "1:4",
        "1:8",
        "2:3",
        "3:2",
        "3:4",
        "4:1",
        "4:3",
        "4:5",
        "5:4",
        "8:1",
        "9:16",
        "16:9",
        "21:9",
    }
)
_GEMINI_IMAGE_SIZES = frozenset({"512", "1K", "2K", "4K"})
_GEMINI_IMAGE_FORMATS = {"image/jpeg": (".jpg", _JPEG_SIGNATURE)}
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_ASSET_LOCKS_GUARD = threading.Lock()
_ASSET_LOCKS: weakref.WeakValueDictionary[
    str, threading.Lock
] = weakref.WeakValueDictionary()
MediaEvidenceBuilder = Callable[[Mapping[str, Any]], Mapping[str, Any] | None]


@dataclass(frozen=True)
class WikiMediaAsset:
    """A generated media asset with source and model provenance."""

    slot_id: str
    kind: str
    uri: str
    mime_type: str
    model: str
    provider: str
    prompt: str
    source_citations: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["source_citations"] = list(self.source_citations)
        return data


class OpenAICompatibleImageGenerator:
    """Generate wiki media through an OpenAI-compatible images endpoint.

    The adapter targets ``POST /images/generations`` and accepts either
    ``b64_json`` or hosted ``url`` responses. It is intentionally isolated from
    page planning so CodeNib can keep deterministic slots even when no media
    provider is configured.
    """

    def __init__(
        self,
        *,
        model: str,
        api_base: str,
        api_key: str | None = None,
        size: str = "1024x1024",
        timeout: float = 120.0,
        urlopen: Callable[..., Any] | None = None,
        provider: str = "openai-compatible",
    ) -> None:
        self.model = str(model or "").strip()
        self.api_base = str(api_base or "").strip()
        self.api_key = _validated_api_key(api_key)
        self.size = _validated_image_size(size)
        self.timeout = _validated_timeout(timeout)
        self.provider = str(provider or "").strip()
        self._urlopen = urlopen or urllib.request.urlopen
        if not self.model:
            raise ValueError("wiki media model is required")
        if len(self.model) > _MAX_MODEL_LENGTH:
            raise ValueError("wiki media model is too long")
        if not self.provider or len(self.provider) > _MAX_PROVIDER_LENGTH:
            raise ValueError("wiki media provider is invalid")
        self._endpoint = _image_generation_endpoint(self.api_base)

    @property
    def endpoint(self) -> str:
        return self._endpoint

    def generate(
        self,
        slot: Mapping[str, Any],
        *,
        output_dir: str | Path | None = None,
        asset_base_path: str = "assets/wiki-media",
        reuse_existing: bool = True,
    ) -> dict[str, Any]:
        slot = _normalized_slot(slot)
        kind = str(slot.get("kind") or "").strip()
        if kind not in _GENERATABLE_IMAGE_KINDS:
            raise ValueError(f"unsupported image media slot kind: {kind!r}")
        slot_id = str(slot.get("id") or "").strip()
        if not slot_id:
            raise ValueError("media slot id is required")
        if output_dir is None:
            raise ValueError("output_dir is required for image generation")

        prompt = _generation_prompt(slot)
        public_prompt = _generation_prompt(slot, include_evidence_pack=False)
        prompt_sha256 = _sha256_text(prompt)
        render_sha256 = _slot_render_sha256(slot)
        filename = f"{_safe_filename(slot_id)}.png"
        with _asset_lock(output_dir, filename):
            cached = _read_cached_asset(
                output_dir,
                filename,
                expected={
                    "slot_id": slot_id,
                    "model": self.model,
                    "provider": self.provider,
                    "prompt_sha256": prompt_sha256,
                    "render_sha256": render_sha256,
                    "size": self.size,
                },
            )
            if cached is not None and reuse_existing:
                return cached

            payload = {
                "model": self.model,
                "prompt": prompt,
                "n": 1,
                "size": self.size,
                "response_format": "b64_json",
            }
            response = self._post_json(payload)
            item = _first_image_response(response)
            citations = _source_citations(slot)
            content_sha256 = None

            if item.get("b64_json"):
                encoded = str(item["b64_json"])
                if len(encoded) > ((_MAX_IMAGE_BYTES + 2) // 3) * 4:
                    raise ValueError("generated image exceeds the byte limit")
                data = base64.b64decode(encoded, validate=True)
                if len(data) > _MAX_IMAGE_BYTES:
                    raise ValueError("generated image exceeds the byte limit")
                if not data.startswith(_PNG_SIGNATURE):
                    raise ValueError("generated image is not a PNG payload")
                target_dir = Path(output_dir)
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / filename
                _atomic_write_bytes(target, data)
                content_sha256 = hashlib.sha256(data).hexdigest()
                uri = f"{asset_base_path.rstrip('/')}/{filename}"
                mime_type = "image/png"
            elif item.get("url"):
                uri = _validated_remote_asset_url(str(item["url"]))
                mime_type = "image/*"
            else:
                raise ValueError("image response must contain b64_json or url")

            asset = WikiMediaAsset(
                slot_id=slot_id,
                kind=kind,
                uri=uri,
                mime_type=mime_type,
                model=self.model,
                provider=self.provider,
                prompt=public_prompt,
                source_citations=citations,
                metadata=_asset_metadata(slot, size=self.size),
            ).to_dict()
            _write_asset_manifest(
                output_dir,
                filename,
                asset,
                prompt_sha256=prompt_sha256,
                render_sha256=render_sha256,
                content_sha256=content_sha256,
            )
            return asset

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
            raw = response.read(_MAX_IMAGE_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_IMAGE_RESPONSE_BYTES:
            raise ValueError("image response exceeds the byte limit")
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("image response must be a JSON object")
        return data


class GeminiInteractionsImageGenerator:
    """Generate Wiki media with Gemini's native Interactions image API.

    This adapter is deliberately separate from the OpenAI-compatible route:
    Gemini uses ``x-goog-api-key``, a typed interaction timeline, and an image
    ``response_format``. Requests opt out of provider-side interaction storage,
    while generated bytes still use CodeNib's local manifest and provenance
    contract.
    """

    provider = "google-gemini"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        api_base: str = _GEMINI_INTERACTIONS_API_BASE,
        aspect_ratio: str = "16:9",
        image_size: str = "1K",
        mime_type: str = "image/jpeg",
        timeout: float = 120.0,
        urlopen: Callable[..., Any] | None = None,
    ) -> None:
        self.model = str(model or "").strip()
        if not self.model:
            raise ValueError("wiki media model is required")
        if len(self.model) > _MAX_MODEL_LENGTH:
            raise ValueError("wiki media model is too long")
        self.api_key = _validated_api_key(api_key)
        if not self.api_key:
            raise ValueError("Gemini wiki media API key is required")
        self.aspect_ratio = _validated_choice(
            aspect_ratio,
            choices=_GEMINI_ASPECT_RATIOS,
            label="Gemini wiki media aspect_ratio",
        )
        self.image_size = _validated_choice(
            image_size,
            choices=_GEMINI_IMAGE_SIZES,
            label="Gemini wiki media image_size",
        )
        self.mime_type = _validated_choice(
            mime_type,
            choices=frozenset(_GEMINI_IMAGE_FORMATS),
            label="Gemini wiki media mime_type",
        )
        self.timeout = _validated_timeout(timeout)
        self._endpoint = _gemini_interactions_endpoint(api_base)
        self._urlopen = urlopen or urllib.request.urlopen

    @property
    def endpoint(self) -> str:
        return self._endpoint

    def generate(
        self,
        slot: Mapping[str, Any],
        *,
        output_dir: str | Path | None = None,
        asset_base_path: str = "assets/wiki-media",
        reuse_existing: bool = True,
    ) -> dict[str, Any]:
        slot = _normalized_slot(slot)
        kind = str(slot.get("kind") or "").strip()
        if kind not in _GENERATABLE_IMAGE_KINDS:
            raise ValueError(f"unsupported image media slot kind: {kind!r}")
        slot_id = str(slot.get("id") or "").strip()
        if not slot_id:
            raise ValueError("media slot id is required")
        if output_dir is None:
            raise ValueError("output_dir is required for image generation")

        prompt = _generation_prompt(slot)
        public_prompt = _generation_prompt(slot, include_evidence_pack=False)
        prompt_sha256 = _sha256_text(prompt)
        render_sha256 = _slot_render_sha256(slot)
        response_format = {
            "type": "image",
            "mime_type": self.mime_type,
            "aspect_ratio": self.aspect_ratio,
            "image_size": self.image_size,
            "delivery": "inline",
        }
        generation_sha256 = _sha256_json(response_format)
        extension, signature = _GEMINI_IMAGE_FORMATS[self.mime_type]
        filename = f"{_safe_filename(slot_id)}{extension}"

        with _asset_lock(output_dir, filename):
            cached = _read_cached_asset(
                output_dir,
                filename,
                expected={
                    "slot_id": slot_id,
                    "model": self.model,
                    "provider": self.provider,
                    "prompt_sha256": prompt_sha256,
                    "render_sha256": render_sha256,
                    "generation_sha256": generation_sha256,
                },
            )
            if cached is not None and reuse_existing:
                return cached

            response = self._post_json(
                {
                    "model": self.model,
                    "input": [{"type": "text", "text": prompt}],
                    "response_format": response_format,
                    "store": False,
                }
            )
            image = _last_gemini_image(response)
            response_mime_type = str(image.get("mime_type") or "").strip().lower()
            if response_mime_type != self.mime_type:
                raise ValueError(
                    "Gemini image response MIME type does not match the request"
                )
            encoded = image.get("data")
            if not isinstance(encoded, str) or not encoded:
                raise ValueError("Gemini image response must contain base64 data")
            if len(encoded) > ((_MAX_IMAGE_BYTES + 2) // 3) * 4:
                raise ValueError("generated image exceeds the byte limit")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError(
                    "Gemini image response contains invalid base64"
                ) from exc
            if len(data) > _MAX_IMAGE_BYTES:
                raise ValueError("generated image exceeds the byte limit")
            if not data.startswith(signature):
                raise ValueError(
                    f"Gemini image response is not a valid {self.mime_type} payload"
                )

            target_dir = Path(output_dir)
            target_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_bytes(target_dir / filename, data)
            citations = _source_citations(slot)
            asset = WikiMediaAsset(
                slot_id=slot_id,
                kind=kind,
                uri=f"{asset_base_path.rstrip('/')}/{filename}",
                mime_type=self.mime_type,
                model=self.model,
                provider=self.provider,
                prompt=public_prompt,
                source_citations=citations,
                metadata=_asset_metadata(
                    slot,
                    aspect_ratio=self.aspect_ratio,
                    image_size=self.image_size,
                    delivery="inline",
                    stored_by_provider=False,
                ),
            ).to_dict()
            _write_asset_manifest(
                output_dir,
                filename,
                asset,
                prompt_sha256=prompt_sha256,
                render_sha256=render_sha256,
                content_sha256=hashlib.sha256(data).hexdigest(),
                generation_sha256=generation_sha256,
            )
            return asset

    def _post_json(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            },
            method="POST",
        )
        with self._urlopen(request, timeout=self.timeout) as response:
            raw = response.read(_MAX_IMAGE_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_IMAGE_RESPONSE_BYTES:
            raise ValueError("image response exceeds the byte limit")
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Gemini image response must be a JSON object")
        return data


class DeterministicSvgMediaGenerator:
    """Generate local, source-grounded SVG previews with the same asset contract.

    This gives CodeNib a zero-credential multimodal path for demos, tests, and
    offline exports. It is deliberately named as a local renderer rather than a
    VLM, so teams can distinguish deterministic scaffolding from provider
    output while keeping the page schema identical.
    """

    model = "local/svg"
    provider = "local"

    def generate(
        self,
        slot: Mapping[str, Any],
        *,
        output_dir: str | Path | None = None,
        asset_base_path: str = "assets/wiki-media",
        reuse_existing: bool = True,
    ) -> dict[str, Any]:
        slot = _normalized_slot(slot)
        kind = str(slot.get("kind") or "").strip()
        if kind not in _GENERATABLE_IMAGE_KINDS:
            raise ValueError(f"unsupported image media slot kind: {kind!r}")
        slot_id = str(slot.get("id") or "").strip()
        if not slot_id:
            raise ValueError("media slot id is required")
        if output_dir is None:
            raise ValueError("output_dir is required for local SVG generation")

        prompt = _generation_prompt(slot)
        public_prompt = _generation_prompt(slot, include_evidence_pack=False)
        prompt_sha256 = _sha256_text(prompt)
        render_sha256 = _slot_render_sha256(slot)
        filename = f"{_safe_filename(slot_id)}.svg"
        with _asset_lock(output_dir, filename):
            cached = _read_cached_asset(
                output_dir,
                filename,
                expected={
                    "slot_id": slot_id,
                    "model": self.model,
                    "provider": self.provider,
                    "prompt_sha256": prompt_sha256,
                    "render_sha256": render_sha256,
                },
            )
            if cached is not None and reuse_existing:
                return cached

            target_dir = Path(output_dir)
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / filename
            svg = _svg_for_slot(slot).encode("utf-8")
            if len(svg) > _MAX_IMAGE_BYTES:
                raise ValueError("generated SVG exceeds the byte limit")
            _atomic_write_bytes(target, svg)
            citations = _source_citations(slot)
            asset = WikiMediaAsset(
                slot_id=slot_id,
                kind=kind,
                uri=f"{asset_base_path.rstrip('/')}/{filename}",
                mime_type="image/svg+xml",
                model=self.model,
                provider=self.provider,
                prompt=public_prompt,
                source_citations=citations,
                metadata=_asset_metadata(slot, deterministic=True),
            ).to_dict()
            _write_asset_manifest(
                output_dir,
                filename,
                asset,
                prompt_sha256=prompt_sha256,
                render_sha256=render_sha256,
                content_sha256=hashlib.sha256(svg).hexdigest(),
            )
            return asset


def materialize_media_slots(
    page: Mapping[str, Any],
    *,
    generator: Any,
    output_dir: str | Path,
    asset_base_path: str = "assets/wiki-media",
    evidence_builder: MediaEvidenceBuilder | None = None,
) -> dict[str, Any]:
    """Generate assets for supported slots and attach them to a page payload."""

    raw_slots = page.get("media_slots") or ()
    if isinstance(raw_slots, (str, bytes, bytearray)):
        raise ValueError("wiki media slots must be a sequence of objects")
    try:
        slots_to_materialize = list(islice(iter(raw_slots), _MAX_MEDIA_SLOTS + 1))
    except TypeError as exc:
        raise ValueError("wiki media slots must be an iterable of objects") from exc
    if len(slots_to_materialize) > _MAX_MEDIA_SLOTS:
        raise ValueError("wiki media slot count exceeds the limit")
    slots = []
    for slot in slots_to_materialize:
        if not isinstance(slot, Mapping):
            continue
        updated = _normalized_slot(slot)
        generation_slot = dict(updated)
        updated.pop("evidence_pack", None)
        if "asset" not in updated:
            kind = str(updated.get("kind") or "")
            if kind in _GENERATABLE_IMAGE_KINDS:
                if (
                    evidence_builder is not None
                    and "evidence_pack" not in generation_slot
                ):
                    evidence_pack = evidence_builder(dict(updated))
                    if evidence_pack is not None:
                        if not isinstance(evidence_pack, Mapping):
                            raise ValueError(
                                "wiki media evidence builder must return a mapping or None"
                            )
                        generation_slot = _normalized_slot(
                            {**updated, "evidence_pack": evidence_pack}
                        )
                updated["asset"] = generator.generate(
                    generation_slot,
                    output_dir=output_dir,
                    asset_base_path=asset_base_path,
                )
        slots.append(updated)
    return {**dict(page), "media_slots": slots}


def redact_media_evidence_packs(page: Mapping[str, Any]) -> dict[str, Any]:
    """Return a public page payload without server-only media evidence."""

    public_page = dict(page)
    raw_slots = public_page.get("media_slots")
    if raw_slots is None:
        return public_page
    if not isinstance(raw_slots, (list, tuple)):
        public_page["media_slots"] = []
        return public_page

    public_slots = []
    for slot in raw_slots:
        if isinstance(slot, Mapping):
            public_slot = dict(slot)
            public_slot.pop("evidence_pack", None)
            public_slots.append(public_slot)
        else:
            public_slots.append(slot)
    public_page["media_slots"] = public_slots
    return public_page


def image_generator_from_config(
    config: Any,
) -> (
    GeminiInteractionsImageGenerator
    | OpenAICompatibleImageGenerator
    | DeterministicSvgMediaGenerator
    | None
):
    """Build a media generator from ``QAConfig``-shaped settings."""

    if not bool(getattr(config, "wiki_media_generation_enabled", False)):
        return None
    model = str(config.wiki_media_model or "").strip()
    options = dict(getattr(config, "wiki_media_options", {}) or {})
    provider = str(options.get("provider") or "").strip().lower()
    if model.lower() in {"local/svg", "local-svg"} or provider in {
        "local",
        "local-svg",
    }:
        return DeterministicSvgMediaGenerator()
    if provider in _GEMINI_PROVIDERS:
        return GeminiInteractionsImageGenerator(
            model=model,
            api_base=str(config.wiki_media_api_base or _GEMINI_INTERACTIONS_API_BASE),
            api_key=getattr(config, "wiki_media_api_key", None),
            aspect_ratio=str(options.get("aspect_ratio") or "16:9"),
            image_size=str(options.get("image_size") or "1K"),
            mime_type=str(options.get("mime_type") or "image/jpeg"),
            timeout=options.get("timeout", 120.0),
        )
    return OpenAICompatibleImageGenerator(
        model=model,
        api_base=str(config.wiki_media_api_base or ""),
        api_key=getattr(config, "wiki_media_api_key", None),
        size=str(options.get("size") or "1024x1024"),
        timeout=options.get("timeout", 120.0),
        provider=str(options.get("provider") or "openai-compatible"),
    )


def read_generated_media_asset(output_dir: str | Path, filename: str) -> bytes | None:
    """Read one generated asset without following links or exceeding its cap."""

    return _read_regular_bytes(Path(output_dir) / filename, max_bytes=_MAX_IMAGE_BYTES)


def _generation_prompt(
    slot: Mapping[str, Any], *, include_evidence_pack: bool = True
) -> str:
    parts = [str(slot.get("prompt") or "").strip()]
    purpose = str(slot.get("purpose") or "").strip()
    if purpose:
        parts.append(f"Purpose: {purpose}")
    citations = _source_citations(slot)
    if citations:
        parts.append("Source citations: " + ", ".join(citations))
    if include_evidence_pack:
        evidence_pack = _evidence_pack_prompt(slot.get("evidence_pack"))
        if evidence_pack:
            parts.append(evidence_pack)
    prompt = "\n\n".join(part for part in parts if part)
    if len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
        raise ValueError("wiki media prompt exceeds the byte limit")
    return prompt


def _evidence_pack_prompt(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    evidence = _serialized_evidence_pack(value)
    return "Source-grounded media evidence pack:\n" + evidence


def _asset_metadata(slot: Mapping[str, Any], **values: Any) -> dict[str, Any]:
    metadata = dict(values)
    if isinstance(slot.get("evidence_pack"), Mapping):
        metadata["evidence_pack_sha256"] = _sha256_text(
            _serialized_evidence_pack(slot["evidence_pack"])
        )
    return metadata


def _serialized_evidence_pack(value: Mapping[str, Any]) -> str:
    try:
        evidence = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (RecursionError, TypeError, ValueError) as exc:
        raise ValueError("wiki media evidence pack must contain bounded JSON") from exc
    if len(evidence.encode("utf-8")) > MAX_MEDIA_EVIDENCE_PACK_BYTES:
        raise ValueError("wiki media evidence pack exceeds the byte limit")
    return evidence


def _source_citations(slot: Mapping[str, Any]) -> tuple[str, ...]:
    citation_values = slot.get("source_citations") or ()
    if isinstance(citation_values, (str, bytes, bytearray)):
        raise ValueError("wiki media source citations must be a sequence")
    citations = []
    for value in citation_values:
        citation = str(value)
        if len(citation.encode("utf-8")) > 4096:
            raise ValueError("wiki media source citation exceeds the byte limit")
        citations.append(citation)
        if len(citations) >= 6:
            break
    return tuple(citations)


def _normalized_slot(slot: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(slot)
    normalized["source_citations"] = list(_source_citations(slot))
    if "evidence_pack" in normalized:
        raw_evidence = normalized["evidence_pack"]
        if raw_evidence is None:
            normalized.pop("evidence_pack")
        elif not isinstance(raw_evidence, Mapping):
            raise ValueError("wiki media evidence pack must be a mapping")
        else:
            evidence_pack = normalize_media_evidence_pack(raw_evidence)
            slot_id = str(normalized.get("id") or "").strip()
            kind = str(normalized.get("kind") or "").strip()
            if evidence_pack["slot_id"] != slot_id:
                raise ValueError(
                    "wiki media evidence pack slot_id does not match the slot"
                )
            if evidence_pack["kind"] != kind:
                raise ValueError(
                    "wiki media evidence pack kind does not match the slot"
                )
            normalized["evidence_pack"] = evidence_pack
    return normalized


def _slot_render_sha256(slot: Mapping[str, Any]) -> str:
    title = str(slot.get("title") or "")
    if len(title.encode("utf-8")) > 4096:
        raise ValueError("wiki media title exceeds the byte limit")
    payload = {
        "id": str(slot.get("id") or ""),
        "kind": str(slot.get("kind") or ""),
        "title": title,
        "purpose": str(slot.get("purpose") or ""),
        "prompt": str(slot.get("prompt") or ""),
        "source_citations": list(_source_citations(slot)),
    }
    encoded = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _first_image_response(response: Mapping[str, Any]) -> Mapping[str, Any]:
    data = response.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], Mapping):
        raise ValueError("image response must contain data[0]")
    return data[0]


def _last_gemini_image(response: Mapping[str, Any]) -> Mapping[str, Any]:
    steps = response.get("steps")
    if not isinstance(steps, list):
        raise ValueError("Gemini image response must contain a steps array")
    images: list[Mapping[str, Any]] = []
    for step in steps:
        if not isinstance(step, Mapping) or step.get("type") != "model_output":
            continue
        content = step.get("content")
        if not isinstance(content, list):
            continue
        images.extend(
            item
            for item in content
            if isinstance(item, Mapping) and item.get("type") == "image"
        )
    if not images:
        raise ValueError("Gemini image response contains no model image output")
    return images[-1]


def _validated_timeout(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("wiki media timeout must be a positive number")
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("wiki media timeout must be a positive number") from exc
    if not math.isfinite(timeout) or not 0 < timeout <= _MAX_TIMEOUT_SECONDS:
        raise ValueError(
            f"wiki media timeout must be between 0 and {_MAX_TIMEOUT_SECONDS:g} seconds"
        )
    return timeout


def _validated_api_key(value: Any) -> str | None:
    if value is None:
        return None
    api_key = str(value)
    if len(api_key) > 8192 or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in api_key
    ):
        raise ValueError("wiki media API key is invalid")
    return api_key


def _validated_image_size(value: Any) -> str:
    size = str(value or "").strip()
    match = re.fullmatch(r"([1-9][0-9]{0,4})x([1-9][0-9]{0,4})", size)
    if match is None:
        raise ValueError("wiki media size must use WIDTHxHEIGHT positive integers")
    if any(int(dimension) > _MAX_IMAGE_DIMENSION for dimension in match.groups()):
        raise ValueError(
            f"wiki media dimensions must not exceed {_MAX_IMAGE_DIMENSION} pixels"
        )
    return size


def _validated_choice(value: Any, *, choices: frozenset[str], label: str) -> str:
    choice = str(value or "").strip()
    if choice not in choices:
        expected = ", ".join(sorted(choices))
        raise ValueError(f"{label} must be one of: {expected}")
    return choice


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


def _image_generation_endpoint(api_base: str) -> str:
    base = _validated_http_url(api_base, label="wiki media api_base")
    parsed = urlsplit(base)
    path = parsed.path.rstrip("/")
    if not path.endswith("/images/generations"):
        path = f"{path}/images/generations"
    return parsed._replace(path=path, fragment="").geturl()


def _gemini_interactions_endpoint(api_base: str) -> str:
    base = _validated_http_url(api_base, label="Gemini wiki media api_base")
    parsed = urlsplit(base)
    if parsed.scheme.lower() != "https":
        raise ValueError("Gemini wiki media api_base must use HTTPS")
    if parsed.query or parsed.fragment:
        raise ValueError(
            "Gemini wiki media api_base must not contain query or fragment"
        )
    path = parsed.path.rstrip("/")
    if not path.endswith("/interactions"):
        path = f"{path}/interactions"
    return parsed._replace(path=path).geturl()


def _validated_remote_asset_url(value: str) -> str:
    return _validated_http_url(value, label="generated image URL")


def _asset_lock(output_dir: str | Path | None, filename: str) -> threading.Lock:
    base = os.path.abspath(os.fspath(output_dir)) if output_dir is not None else ""
    key = os.path.join(base, filename)
    with _ASSET_LOCKS_GUARD:
        lock = _ASSET_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _ASSET_LOCKS[key] = lock
        return lock


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_regular_bytes(path: Path, *, max_bytes: int) -> bytes | None:
    try:
        before = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
        return None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size > max_bytes
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            return None
        chunks = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        finished = os.fstat(descriptor)
        if (
            finished.st_size,
            finished.st_mtime_ns,
            finished.st_ctime_ns,
        ) != (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns):
            return None
        payload = b"".join(chunks)
        return payload if len(payload) <= max_bytes else None
    finally:
        os.close(descriptor)


def _safe_filename(value: str) -> str:
    if len(value.encode("utf-8")) > 4096:
        raise ValueError("media slot id exceeds the byte limit")
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    name = name or "wiki-media"
    stem = name.split(".", 1)[0].upper()
    if name != value or len(name) > 128 or stem in _WINDOWS_RESERVED_NAMES:
        digest = _sha256_text(value)[:12]
        name = f"{name[:96].rstrip('.-') or 'wiki-media'}-{digest}"
    return name


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _manifest_path(output_dir: str | Path | None, filename: str) -> Path | None:
    if output_dir is None:
        return None
    return Path(output_dir) / f"{filename}.json"


def _read_cached_asset(
    output_dir: str | Path | None,
    filename: str,
    *,
    expected: Mapping[str, Any],
) -> dict[str, Any] | None:
    manifest_path = _manifest_path(output_dir, filename)
    if manifest_path is None:
        return None
    raw_manifest = _read_regular_bytes(manifest_path, max_bytes=_MAX_MANIFEST_BYTES)
    if raw_manifest is None:
        return None
    try:
        payload = json.loads(raw_manifest.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if any(payload.get(key) != value for key, value in expected.items()):
        return None
    asset = payload.get("asset")
    if not isinstance(asset, Mapping):
        return None
    content_sha256 = payload.get("content_sha256")
    if content_sha256 is None:
        try:
            _validated_remote_asset_url(str(asset.get("uri") or ""))
        except ValueError:
            return None
    elif isinstance(content_sha256, str) and re.fullmatch(
        r"[0-9a-f]{64}", content_sha256
    ):
        asset_path = Path(output_dir) / filename
        content = _read_regular_bytes(asset_path, max_bytes=_MAX_IMAGE_BYTES)
        if content is None or hashlib.sha256(content).hexdigest() != content_sha256:
            return None
    else:
        return None
    return dict(asset)


def _write_asset_manifest(
    output_dir: str | Path | None,
    filename: str,
    asset: Mapping[str, Any],
    *,
    prompt_sha256: str,
    render_sha256: str,
    content_sha256: str | None,
    generation_sha256: str | None = None,
) -> None:
    manifest_path = _manifest_path(output_dir, filename)
    if manifest_path is None:
        return
    payload = {
        "slot_id": asset.get("slot_id"),
        "model": asset.get("model"),
        "provider": asset.get("provider"),
        "prompt_sha256": prompt_sha256,
        "render_sha256": render_sha256,
        "size": (asset.get("metadata") or {}).get("size"),
        "content_sha256": content_sha256,
        "asset": dict(asset),
    }
    if generation_sha256 is not None:
        payload["generation_sha256"] = generation_sha256
    encoded = (
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if len(encoded) > _MAX_MANIFEST_BYTES:
        raise ValueError("wiki media manifest exceeds the byte limit")
    _atomic_write_bytes(
        manifest_path,
        encoded,
    )


def _svg_for_slot(slot: Mapping[str, Any]) -> str:
    title = html.escape(str(slot.get("title") or "Wiki media"))
    kind = html.escape(str(slot.get("kind") or "media").title())
    purpose = str(slot.get("purpose") or "Source-grounded visual")
    citations = _source_citations(slot)[:4]
    files = list(citations) or ["source evidence"]
    palette = {
        "diagram": ("#2563eb", "#eff6ff"),
        "image": ("#7c3aed", "#f5f3ff"),
        "storyboard": ("#0891b2", "#ecfeff"),
    }
    accent, wash = palette.get(str(slot.get("kind") or ""), ("#2563eb", "#eff6ff"))
    kind_value = str(slot.get("kind") or "")
    prompt_hash = _sha256_text(_generation_prompt(slot))[:12]
    if kind_value == "diagram":
        body = _svg_diagram_body(files, accent, wash)
    elif kind_value == "storyboard":
        body = _svg_storyboard_body(files, accent, wash)
    else:
        body = _svg_concept_body(files, accent, wash)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540" viewBox="0 0 960 540" role="img" aria-label="{title}">
  <defs>
    <linearGradient id="bg" x1="0" x2="1" y1="0" y2="1">
      <stop offset="0" stop-color="{wash}"/>
      <stop offset="1" stop-color="#ffffff"/>
    </linearGradient>
    <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="12" stdDeviation="18" flood-color="#0f172a" flood-opacity="0.14"/>
    </filter>
  </defs>
  <rect width="960" height="540" fill="url(#bg)"/>
  <circle cx="828" cy="84" r="94" fill="{accent}" opacity="0.11"/>
  <circle cx="112" cy="468" r="132" fill="{accent}" opacity="0.08"/>
  <g filter="url(#shadow)">
    <rect x="56" y="56" width="848" height="428" rx="28" fill="#ffffff" stroke="#dbe3ef"/>
  </g>
  <rect x="84" y="86" width="96" height="30" rx="15" fill="{wash}" stroke="{accent}"/>
  <text x="104" y="106" font-size="14" fill="{accent}" font-weight="750" font-family="ui-sans-serif, system-ui">{kind}</text>
  <text x="84" y="152" font-size="31" fill="#0f172a" font-weight="780" font-family="ui-sans-serif, system-ui">{title}</text>
  {_svg_multiline_text(purpose, x=84, y=180, width=760, max_lines=2, font_size=15, color="#475569")}
  {body}
  <rect x="84" y="448" width="792" height="22" rx="11" fill="#f8fafc" stroke="#e2e8f0"/>
  <text x="100" y="464" font-size="12" fill="#64748b" font-family="ui-sans-serif, system-ui">local/svg · source-grounded media slot · prompt sha256:{prompt_hash} · provider-neutral asset contract</text>
</svg>
"""


def _svg_diagram_body(files: list[str], accent: str, wash: str) -> str:
    cards = [
        ("Source", "cited files"),
        ("Facts", "graph/LSP"),
        ("Plan", "media slot"),
        ("Asset", "wiki media"),
    ]
    card_markup = []
    for index, (label, detail) in enumerate(cards):
        x = 92 + index * 202
        card_markup.append(
            f'<rect x="{x}" y="252" width="154" height="88" rx="20" fill="{wash}" '
            f'stroke="{accent}" stroke-width="1.5"/>'
            f'<circle cx="{x + 28}" cy="281" r="12" fill="{accent}" opacity="0.18"/>'
            f'<text x="{x + 48}" y="286" font-size="18" fill="#0f172a" '
            f'font-weight="760" font-family="ui-sans-serif, system-ui">{label}</text>'
            f'<text x="{x + 24}" y="318" font-size="13" fill="#475569" '
            f'font-family="ui-sans-serif, system-ui">{detail}</text>'
        )
        if index < len(cards) - 1:
            ax = x + 154
            card_markup.append(
                f'<path d="M{ax + 10} 296 L{ax + 44} 296" stroke="{accent}" '
                f'stroke-width="3.5" stroke-linecap="round"/>'
                f'<path d="M{ax + 38} 288 L{ax + 48} 296 L{ax + 38} 304" '
                f'stroke="{accent}" stroke-width="3.5" fill="none" '
                f'stroke-linecap="round" stroke-linejoin="round"/>'
            )
    citation_markup = _svg_citation_chips(files, x=92, y=370, width=760)
    return "\n  ".join(
        [
            '<text x="92" y="230" font-size="14" fill="#64748b" '
            'font-weight="700" font-family="ui-sans-serif, system-ui">'
            "Deterministic planning path</text>",
            *card_markup,
            citation_markup,
        ]
    )


def _svg_concept_body(files: list[str], accent: str, wash: str) -> str:
    return f"""
  <rect x="92" y="236" width="360" height="178" rx="22" fill="#0f172a"/>
  <rect x="116" y="264" width="184" height="12" rx="6" fill="#94a3b8"/>
  <rect x="116" y="294" width="284" height="10" rx="5" fill="#38bdf8" opacity="0.84"/>
  <rect x="116" y="320" width="224" height="10" rx="5" fill="#a78bfa" opacity="0.90"/>
  <rect x="116" y="346" width="260" height="10" rx="5" fill="#34d399" opacity="0.86"/>
  <rect x="116" y="372" width="198" height="10" rx="5" fill="#fbbf24" opacity="0.82"/>
  <text x="116" y="446" font-size="13" fill="#64748b" font-family="ui-sans-serif, system-ui">source code stays the technical source of truth</text>
  <path d="M474 324 C526 272 572 272 624 324" stroke="{accent}" stroke-width="4" fill="none" stroke-linecap="round"/>
  <path d="M612 314 L626 324 L612 334" stroke="{accent}" stroke-width="4" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
  <rect x="642" y="236" width="210" height="178" rx="28" fill="{wash}" stroke="{accent}" stroke-width="1.5"/>
  <circle cx="708" cy="302" r="38" fill="{accent}" opacity="0.18"/>
  <path d="M684 304 L704 324 L738 282" stroke="{accent}" stroke-width="7" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
  <text x="684" y="364" font-size="20" fill="#0f172a" font-weight="780" font-family="ui-sans-serif, system-ui">teaching visual</text>
  <text x="684" y="388" font-size="13" fill="#475569" font-family="ui-sans-serif, system-ui">bounded by citations</text>
  {_svg_citation_chips(files, x=92, y=204, width=760)}
"""


def _svg_storyboard_body(files: list[str], accent: str, wash: str) -> str:
    panels = [
        ("1", "Evidence", "collect cited files"),
        ("2", "Relations", "trace graph facts"),
        ("3", "Narrative", "shape teaching flow"),
        ("4", "Asset", "render local media"),
    ]
    markup = [
        '<text x="92" y="226" font-size="14" fill="#64748b" '
        'font-weight="700" font-family="ui-sans-serif, system-ui">'
        "Storyboard for future video generation</text>"
    ]
    for index, (num, title, detail) in enumerate(panels):
        x = 92 + index * 196
        markup.append(
            f'<rect x="{x}" y="250" width="166" height="136" rx="22" fill="{wash}" '
            f'stroke="{accent}" stroke-width="1.5"/>'
            f'<circle cx="{x + 34}" cy="286" r="17" fill="{accent}"/>'
            f'<text x="{x + 29}" y="292" font-size="17" fill="#ffffff" '
            f'font-weight="800" font-family="ui-sans-serif, system-ui">{num}</text>'
            f'<text x="{x + 24}" y="330" font-size="18" fill="#0f172a" '
            f'font-weight="760" font-family="ui-sans-serif, system-ui">{title}</text>'
            f'<text x="{x + 24}" y="356" font-size="13" fill="#475569" '
            f'font-family="ui-sans-serif, system-ui">{detail}</text>'
        )
        if index < len(panels) - 1:
            markup.append(
                f'<path d="M{x + 170} 318 L{x + 190} 318" stroke="{accent}" '
                f'stroke-width="3" stroke-linecap="round"/>'
            )
    markup.append(_svg_citation_chips(files, x=92, y=410, width=760))
    return "\n  ".join(markup)


def _svg_citation_chips(files: list[str], *, x: int, y: int, width: int) -> str:
    chips = [
        '<text x="{x}" y="{y}" font-size="13" fill="#64748b" font-weight="700" '
        'font-family="ui-sans-serif, system-ui">Source citations</text>'.format(
            x=x, y=y
        )
    ]
    cursor_x = x
    cursor_y = y + 18
    for file in files[:4]:
        label = _svg_shorten(file, 34)
        chip_width = min(width, max(132, len(label) * 8 + 30))
        if cursor_x + chip_width > x + width:
            cursor_x = x
            cursor_y += 32
        chips.append(
            f'<rect x="{cursor_x}" y="{cursor_y}" width="{chip_width}" height="24" '
            f'rx="12" fill="#f8fafc" stroke="#dbe3ef"/>'
            f'<text x="{cursor_x + 14}" y="{cursor_y + 17}" font-size="12" '
            f'fill="#334155" font-family="ui-monospace, SFMono-Regular, Menlo, monospace">'
            f"{html.escape(label)}</text>"
        )
        cursor_x += chip_width + 10
    return "\n  ".join(chips)


def _svg_multiline_text(
    text: str,
    *,
    x: int,
    y: int,
    width: int,
    max_lines: int,
    font_size: int,
    color: str,
) -> str:
    max_chars = max(24, width // max(1, font_size // 2))
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines and words:
        consumed = " ".join(lines)
        if len(consumed) < len(text):
            lines[-1] = _svg_shorten(lines[-1], max_chars - 1) + "…"
    return "\n  ".join(
        f'<text x="{x}" y="{y + index * (font_size + 7)}" font-size="{font_size}" '
        f'fill="{color}" font-family="ui-sans-serif, system-ui">'
        f"{html.escape(line)}</text>"
        for index, line in enumerate(lines)
    )


def _svg_shorten(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return "…" + value[-(limit - 1) :]


__all__ = [
    "DeterministicSvgMediaGenerator",
    "GeminiInteractionsImageGenerator",
    "OpenAICompatibleImageGenerator",
    "WikiMediaAsset",
    "image_generator_from_config",
    "materialize_media_slots",
    "read_generated_media_asset",
]
