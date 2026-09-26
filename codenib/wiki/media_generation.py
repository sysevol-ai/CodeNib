# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Provider-neutral materialization for planned wiki media slots."""

from __future__ import annotations

import base64
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
from xml.etree import ElementTree

from .media_evidence import MAX_MEDIA_EVIDENCE_PACK_BYTES, normalize_media_evidence_pack
from .visual_ir import (
    VISUAL_ADAPTER_MEDIA_KIND,
    normalize_render_contract,
    page_visual_contract_report,
    page_visual_evidence_refs,
    render_contract_report,
)

_GENERATABLE_IMAGE_KINDS = frozenset({"diagram", "image", "storyboard", "chart"})
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
_LOCAL_SVG_DESIGN_VERSION = 5
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_ASSET_LOCKS_GUARD = threading.Lock()
_ASSET_LOCKS: weakref.WeakValueDictionary[str, threading.Lock] = (
    weakref.WeakValueDictionary()
)
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

    def supports(self, slot: Mapping[str, Any]) -> bool:
        """Return whether this image provider can materialize the slot kind."""

        return str(slot.get("kind") or "").strip() in _GENERATABLE_IMAGE_KINDS

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

            metadata: dict[str, Any] = _asset_metadata(slot, size=self.size)
            if "render_contract" in slot:
                metadata.update(
                    {
                        "adapter": slot["render_contract"]["adapter"],
                        "visual_schema_version": slot["render_contract"][
                            "schema_version"
                        ],
                    }
                )
            asset = WikiMediaAsset(
                slot_id=slot_id,
                kind=kind,
                uri=uri,
                mime_type=mime_type,
                model=self.model,
                provider=self.provider,
                prompt=public_prompt,
                source_citations=citations,
                metadata=metadata,
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


class DeterministicSvgMediaGenerator:
    """Generate local, source-grounded SVG previews with the same asset contract.

    This gives CodeNib a zero-credential multimodal path for demos, tests, and
    offline exports. It is deliberately named as a local renderer rather than a
    VLM, so teams can distinguish deterministic scaffolding from provider
    output while keeping the page schema identical.
    """

    model = "local/svg"
    provider = "local"

    def supports(self, slot: Mapping[str, Any]) -> bool:
        """Render only visuals whose complete content is in a typed contract."""

        if str(slot.get("kind") or "").strip() not in _GENERATABLE_IMAGE_KINDS:
            return False
        try:
            normalize_render_contract(slot.get("render_contract"))
        except (TypeError, ValueError):
            return False
        return True

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
        if not self.supports(slot):
            raise ValueError(
                "local SVG rendering requires a valid typed render contract"
            )
        slot_id = str(slot.get("id") or "").strip()
        if not slot_id:
            raise ValueError("media slot id is required")
        if output_dir is None:
            raise ValueError("output_dir is required for local SVG generation")

        prompt = _generation_prompt(slot)
        public_prompt = _generation_prompt(slot, include_evidence_pack=False)
        prompt_sha256 = _sha256_text(prompt)
        render_sha256 = _sha256_text(
            f"editorial-svg-v{_LOCAL_SVG_DESIGN_VERSION}:"
            f"{_slot_render_sha256(slot)}"
        )
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
            svg_text = _svg_for_slot(slot)
            _assert_well_formed_svg(svg_text)
            svg = svg_text.encode("utf-8")
            if len(svg) > _MAX_IMAGE_BYTES:
                raise ValueError("generated SVG exceeds the byte limit")
            _atomic_write_bytes(target, svg)
            asset = _deterministic_svg_asset(
                slot,
                uri=f"{asset_base_path.rstrip('/')}/{filename}",
                prompt=public_prompt,
            )
            _write_asset_manifest(
                output_dir,
                filename,
                asset,
                prompt_sha256=prompt_sha256,
                render_sha256=render_sha256,
                content_sha256=hashlib.sha256(svg).hexdigest(),
            )
            return asset


def _assert_well_formed_svg(svg: str) -> None:
    """Refuse to persist an SVG a browser would stop rendering half-way.

    One unescaped ``&`` in a label makes the whole document an XML parse
    error, and the reader sees "This page contains the following errors"
    above a truncated drawing. Catching it here keeps the page loading
    without media instead of publishing the broken asset.
    """

    try:
        ElementTree.fromstring(svg)
    except ElementTree.ParseError as exc:
        raise ValueError(f"generated SVG is not well-formed XML: {exc}") from exc


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
    allowed_evidence = page_visual_evidence_refs(page)
    has_repository_lead = any(
        isinstance(slot, Mapping)
        and str(slot.get("placement") or "") == "lead"
        and isinstance(slot.get("asset"), Mapping)
        and str((slot.get("asset") or {}).get("provider") or "") == "repository"
        for slot in slots_to_materialize
    )
    slots = []
    rejected_contracts = []
    for slot in slots_to_materialize:
        if not isinstance(slot, Mapping):
            continue
        try:
            updated = _normalized_slot(
                slot,
                allowed_evidence=allowed_evidence,
            )
        except (TypeError, ValueError) as exc:
            if "render_contract" not in slot:
                raise
            report = render_contract_report(
                slot.get("render_contract"),
                allowed_evidence=allowed_evidence,
            )
            report.update(
                {
                    "slot_id": str(slot.get("id") or ""),
                    "materialized": False,
                    "rejected": True,
                }
            )
            if not report["errors"]:
                report["errors"] = [str(exc)]
            rejected_contracts.append(report)
            continue
        # The evidence pack is generation input only; the public slot never
        # carries it.
        generation_slot = dict(updated)
        updated.pop("evidence_pack", None)
        if "asset" not in updated:
            supports = getattr(generator, "supports", None)
            supported = (
                bool(supports(updated))
                if callable(supports)
                else str(updated.get("kind") or "") in _GENERATABLE_IMAGE_KINDS
            )
            if has_repository_lead and str(updated.get("placement") or "") == "lead":
                supported = False
            if supported:
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
                            {**updated, "evidence_pack": evidence_pack},
                            allowed_evidence=allowed_evidence,
                        )
                updated["asset"] = generator.generate(
                    generation_slot,
                    output_dir=output_dir,
                    asset_base_path=asset_base_path,
                )
        slots.append(updated)
    result = {**dict(page), "media_slots": slots}
    visual_quality = page_visual_contract_report(result)
    if rejected_contracts:
        visual_quality["valid"] = False
        visual_quality["publication_ready"] = False
        if (
            "one or more typed visual contracts are invalid"
            not in visual_quality["errors"]
        ):
            visual_quality["errors"].insert(
                0,
                "one or more typed visual contracts are invalid",
            )
        visual_quality["typed_slots"] += len(rejected_contracts)
        visual_quality["contracts"].extend(rejected_contracts)
        visual_quality["rejected_slots"] = [
            report["slot_id"] for report in rejected_contracts
        ]
    result["visual_quality"] = visual_quality
    return result


def materialize_deterministic_svg_slots(
    page: Mapping[str, Any],
    *,
    asset_base_path: str,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Materialize typed local SVGs in memory for an atomic static export."""

    renderer = DeterministicSvgMediaGenerator()
    assets: dict[str, bytes] = {}

    class MemoryGenerator:
        def supports(self, slot: Mapping[str, Any]) -> bool:
            return renderer.supports(slot)

        def generate(
            self,
            slot: Mapping[str, Any],
            *,
            output_dir: str | Path | None = None,
            asset_base_path: str = "assets/wiki-media",
        ) -> dict[str, Any]:
            del output_dir
            normalized = _normalized_slot(slot)
            slot_id = str(normalized.get("id") or "").strip()
            if not slot_id:
                raise ValueError("media slot id is required")
            filename = f"{_safe_filename(slot_id)}.svg"
            uri = f"{asset_base_path.rstrip('/')}/{filename}"
            svg = _svg_for_slot(normalized).encode("utf-8")
            if len(svg) > _MAX_IMAGE_BYTES:
                raise ValueError("generated SVG exceeds the byte limit")
            assets[uri] = svg
            return _deterministic_svg_asset(
                normalized,
                uri=uri,
                prompt=_generation_prompt(normalized, include_evidence_pack=False),
            )

    materialized = materialize_media_slots(
        page,
        generator=MemoryGenerator(),
        output_dir="",
        asset_base_path=asset_base_path,
    )
    return materialized, assets


def _deterministic_svg_asset(
    slot: Mapping[str, Any],
    *,
    uri: str,
    prompt: str,
) -> dict[str, Any]:
    contract = normalize_render_contract(slot.get("render_contract"))
    return WikiMediaAsset(
        slot_id=str(slot.get("id") or ""),
        kind=str(slot.get("kind") or ""),
        uri=uri,
        mime_type="image/svg+xml",
        model=DeterministicSvgMediaGenerator.model,
        provider=DeterministicSvgMediaGenerator.provider,
        prompt=prompt,
        source_citations=_source_citations(slot),
        metadata={
            **_asset_metadata(slot, deterministic=True),
            "adapter": contract["adapter"],
            "visual_schema_version": contract["schema_version"],
            "design_system": f"editorial-svg-v{_LOCAL_SVG_DESIGN_VERSION}",
        },
    ).to_dict()


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
) -> OpenAICompatibleImageGenerator | DeterministicSvgMediaGenerator | None:
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
    contract = slot.get("render_contract")
    if contract is not None:
        normalized = normalize_render_contract(contract)
        parts.append(
            "Validated render contract (render exactly; do not infer missing "
            "elements):\n"
            + json.dumps(
                normalized,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
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


def _normalized_slot(
    slot: Mapping[str, Any],
    *,
    allowed_evidence: set[str] | None = None,
) -> dict[str, Any]:
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
    if "render_contract" in slot:
        contract = normalize_render_contract(
            slot.get("render_contract"),
            allowed_evidence=allowed_evidence,
        )
        kind = str(slot.get("kind") or "").strip()
        expected_kind = VISUAL_ADAPTER_MEDIA_KIND[contract["adapter"]]
        if kind != expected_kind:
            raise ValueError(
                f"visual adapter {contract['adapter']!r} requires media kind "
                f"{expected_kind!r}"
            )
        normalized["render_contract"] = contract
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
        "render_contract": slot.get("render_contract"),
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
    contract = normalize_render_contract(slot.get("render_contract"))
    adapter = contract["adapter"]
    if adapter == "architecture" and contract["provenance"] == "architecture-plan":
        return _svg_system_architecture(slot, contract)
    raw_title = str(slot.get("title") or "Wiki media")
    title = html.escape(_svg_truncate(raw_title, 54), quote=True)
    kind = html.escape(adapter.replace("-", " ").upper())
    accent = "#1264d8"
    paper = "#f4f1e9"
    deck = _svg_contract_deck(adapter, contract["data"])
    renderers = {
        "architecture": _svg_architecture_body,
        "flow": _svg_flow_body,
        "storyboard": _svg_storyboard_contract_body,
        "bar-chart": _svg_bar_chart_body,
    }
    body = renderers[adapter](
        contract["data"],
        contract["evidence"],
        accent,
        paper,
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540" viewBox="0 0 960 540" role="img" aria-label="{title}">
  <defs>
    <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 z" fill="{accent}"/>
    </marker>
  </defs>
  <rect width="960" height="540" fill="{paper}"/>
  <rect x="24" y="24" width="912" height="492" fill="#fbfaf6" stroke="#cbc7bd"/>
  <path d="M56 58 H76" stroke="{accent}" stroke-width="4"/>
  <text x="88" y="62" font-size="11" fill="#3d3d38" font-weight="700" letter-spacing="2.1" font-family="ui-sans-serif, system-ui">CODENIB / {kind}</text>
  <text x="56" y="118" font-size="34" fill="#1d1d1a" font-weight="700" font-family="Georgia, Times New Roman, serif">{title}</text>
  <text x="56" y="149" font-size="13" fill="#65645d" letter-spacing="0.2" font-family="ui-sans-serif, system-ui">{html.escape(deck)}</text>
  <path d="M56 176 H904" stroke="#cbc7bd" stroke-width="1"/>
  {body}
  <path d="M56 486 H904" stroke="#cbc7bd" stroke-width="1"/>
  <text x="56" y="505" font-size="9" fill="#737169" letter-spacing="1.45" font-family="ui-sans-serif, system-ui">SOURCE-GROUNDED / EDITORIAL SVG V{_LOCAL_SVG_DESIGN_VERSION}</text>
  <text x="904" y="505" font-size="9" fill="#737169" text-anchor="end" letter-spacing="1.1" font-family="ui-sans-serif, system-ui">{len(contract['evidence'])} SOURCE REFERENCE{'' if len(contract['evidence']) == 1 else 'S'}</text>
</svg>
"""


def _svg_system_architecture(
    slot: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> str:
    """Render an Overview synthesis as roles, path, and explicit boundaries."""

    data = contract["data"]
    nodes = {str(node["id"]): node for node in data["nodes"]}
    path = [nodes[item] for item in data["primary_path"]]
    path_ids = {str(node["id"]) for node in path}
    supporting = [node for node in data["nodes"] if str(node["id"]) not in path_ids]
    boundaries = list(data.get("boundaries") or [])
    edges = list(data["edges"])

    path_height = len(path) * 68 + max(0, len(path) - 1) * 44
    context_height = len(supporting) * 94 + len(boundaries) * 106 + 54
    content_height = max(path_height, context_height, 390)
    canvas_height = max(720, 310 + content_height)
    footer_rule_y = canvas_height - 55
    footer_y = canvas_height - 34
    title = html.escape(
        _svg_truncate(str(slot.get("title") or "System architecture"), 54),
        quote=True,
    )
    deck = _svg_contract_deck("architecture", data)
    accent = "#1264d8"
    layer_colors = {
        "external": "#7c3aed",
        "interface": accent,
        "coordination": "#c26715",
        "execution": "#087f5b",
        "data": "#9a5b13",
    }
    layer_labels = {
        "external": "OUTSIDE THE SYSTEM",
        "interface": "INTERFACE",
        "coordination": "COORDINATION",
        "execution": "EXECUTION",
        "data": "DATA & ARTIFACTS",
    }

    path_markup = []
    card_x = 56
    card_width = 556
    card_y = 214
    for index, node in enumerate(path):
        layer = str(node.get("layer") or "")
        color = layer_colors.get(layer, accent)
        path_markup.append(
            f'<rect x="{card_x}" y="{card_y}" width="{card_width}" height="68" '
            'fill="#fbfaf6" stroke="#cbc7bd"/>'
            f'<rect x="{card_x}" y="{card_y}" width="4" height="68" fill="{color}"/>'
            f'<text x="76" y="{card_y + 42}" font-size="23" fill="{accent}" '
            'font-style="italic" font-family="Georgia, Times New Roman, serif">'
            f"{index + 1:02d}</text>"
            f'<text x="128" y="{card_y + 17}" font-size="8" fill="#77736a" '
            'font-weight="750" letter-spacing="1.2" '
            'font-family="ui-sans-serif, system-ui">'
            f'{html.escape(layer_labels.get(layer, "SYSTEM ROLE"))}</text>'
            f'<text x="128" y="{card_y + 39}" font-size="17" fill="#242420" '
            'font-weight="680" font-family="Georgia, Times New Roman, serif">'
            f'{html.escape(_svg_truncate(node["label"], 42))}</text>'
            f'<text x="128" y="{card_y + 57}" font-size="9.5" fill="#5f5c55" '
            'font-family="ui-sans-serif, system-ui">'
            f'{html.escape(_svg_truncate(node["detail"], 82))}</text>'
        )
        if index < len(path) - 1:
            next_id = str(path[index + 1]["id"])
            edge = next(
                item
                for item in edges
                if str(item["source"]) == str(node["id"])
                and str(item["target"]) == next_id
            )
            path_markup.append(
                f'<path d="M84 {card_y + 68} V{card_y + 108}" '
                'stroke="#aaa79f" stroke-width="1.2" fill="none" '
                'marker-end="url(#architecture-arrow)"/>'
                f'<text x="128" y="{card_y + 94}" font-size="9" fill="#55544f" '
                'font-weight="700" letter-spacing="0.45" '
                'font-family="ui-sans-serif, system-ui">'
                f'{html.escape(_svg_truncate(edge["label"].upper(), 54))}</text>'
            )
            card_y += 112

    context_markup = []
    context_x = 654
    context_y = 214
    if supporting:
        context_markup.append(
            f'<text x="{context_x}" y="{context_y}" font-size="9" fill="#77736a" '
            'font-weight="750" letter-spacing="1.25" '
            'font-family="ui-sans-serif, system-ui">SUPPORTING ROLES</text>'
        )
        context_y += 15
        for node in supporting:
            layer = str(node.get("layer") or "")
            edge = next(
                (
                    item
                    for item in edges
                    if str(item["source"]) == str(node["id"])
                    or str(item["target"]) == str(node["id"])
                ),
                None,
            )
            relationship = ""
            if edge is not None:
                outgoing = str(edge["source"]) == str(node["id"])
                peer_id = str(edge["target"] if outgoing else edge["source"])
                peer = nodes.get(peer_id)
                if peer is not None:
                    if outgoing:
                        relationship = f'{edge["label"]} → {peer["label"]}'
                    else:
                        relationship = f'{peer["label"]} → {edge["label"]}'
            context_markup.append(
                f'<path d="M{context_x} {context_y} H904" stroke="#cbc7bd"/>'
                f'<text x="{context_x}" y="{context_y + 17}" font-size="8" '
                f'fill="{layer_colors.get(layer, accent)}" font-weight="750" '
                'letter-spacing="1.05" font-family="ui-sans-serif, system-ui">'
                f'{html.escape(layer_labels.get(layer, "SYSTEM ROLE"))}</text>'
                f'<text x="{context_x}" y="{context_y + 39}" font-size="15" '
                'fill="#242420" font-weight="680" '
                'font-family="Georgia, Times New Roman, serif">'
                f'{html.escape(_svg_truncate(node["label"], 31))}</text>'
                + _svg_multiline_text(
                    str(node["detail"]),
                    x=context_x,
                    y=context_y + 57,
                    width=250,
                    max_lines=2,
                    font_size=9,
                    color="#5f5c55",
                )
                + (
                    f'<text x="{context_x}" y="{context_y + 86}" font-size="8" '
                    f'fill="{accent}" font-weight="700" '
                    'font-family="ui-sans-serif, system-ui">'
                    f"{html.escape(_svg_truncate(relationship, 46))}</text>"
                    if relationship
                    else ""
                )
            )
            context_y += 94

    if boundaries:
        context_y += 17 if supporting else 0
        context_markup.append(
            f'<text x="{context_x}" y="{context_y}" font-size="9" fill="#77736a" '
            'font-weight="750" letter-spacing="1.25" '
            'font-family="ui-sans-serif, system-ui">EXPLICIT BOUNDARIES</text>'
        )
        context_y += 14
        for boundary in boundaries:
            member_labels = [
                str(nodes[item]["label"])
                for item in boundary["members"]
                if item in nodes
            ]
            context_markup.append(
                f'<rect x="{context_x}" y="{context_y}" width="250" height="92" '
                'fill="#f8f6ef" stroke="#a7a299" stroke-dasharray="5 4"/>'
                f'<rect x="{context_x + 12}" y="{context_y + 13}" width="4" '
                'height="66" fill="#c26715"/>'
                f'<text x="{context_x + 28}" y="{context_y + 25}" font-size="14" '
                'fill="#242420" font-weight="680" '
                'font-family="Georgia, Times New Roman, serif">'
                f'{html.escape(_svg_truncate(boundary["label"], 28))}</text>'
                + _svg_multiline_text(
                    str(boundary.get("detail") or ""),
                    x=context_x + 28,
                    y=context_y + 43,
                    width=205,
                    max_lines=2,
                    font_size=8,
                    color="#5f5c55",
                )
                + f'<text x="{context_x + 28}" y="{context_y + 80}" font-size="8" '
                f'fill="{accent}" font-weight="700" '
                'font-family="ui-sans-serif, system-ui">'
                f'{html.escape(_svg_truncate(" · ".join(member_labels), 42))}</text>'
            )
            context_y += 106

    source_label = "SOURCES  " + " / ".join(contract["evidence"][:8])
    if len(contract["evidence"]) > 8:
        source_label += f" / +{len(contract['evidence']) - 8}"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="960" height="{canvas_height}" viewBox="0 0 960 {canvas_height}" role="img" aria-label="{title}">
  <defs>
    <marker id="architecture-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
      <path d="M 0 0 L 10 5 L 0 10 z" fill="{accent}"/>
    </marker>
  </defs>
  <rect width="960" height="{canvas_height}" fill="#f4f1e9"/>
  <circle cx="870" cy="84" r="118" fill="#1264d8" opacity="0.045"/>
  <rect x="24" y="24" width="912" height="{canvas_height - 48}" fill="#fbfaf6" fill-opacity="0.54" stroke="#cbc7bd"/>
  <path d="M56 58 H76" stroke="{accent}" stroke-width="4"/>
  <text x="88" y="62" font-size="11" fill="#3d3d38" font-weight="700" letter-spacing="2.1" font-family="ui-sans-serif, system-ui">CODENIB / SYSTEM ARCHITECTURE</text>
  <text x="56" y="118" font-size="34" fill="#1d1d1a" font-weight="700" font-family="Georgia, Times New Roman, serif">{title}</text>
  <text x="56" y="149" font-size="13" fill="#65645d" letter-spacing="0.2" font-family="ui-sans-serif, system-ui">{html.escape(deck)}</text>
  <path d="M56 176 H904" stroke="#aaa79f" stroke-width="1"/>
  <text x="56" y="201" font-size="9" fill="#77736a" font-weight="750" letter-spacing="1.25" font-family="ui-sans-serif, system-ui">PRIMARY RUNTIME PATH / INPUT → OUTCOME</text>
  <path d="M632 196 V{footer_rule_y - 18}" stroke="#cbc7bd" stroke-width="1"/>
  {' '.join(path_markup)}
  {' '.join(context_markup)}
  <text x="56" y="{footer_rule_y - 17}" font-size="9" fill="#737169" letter-spacing="0.65" font-family="ui-monospace, SFMono-Regular, Menlo, monospace">{html.escape(_svg_truncate(source_label, 126))}</text>
  <path d="M56 {footer_rule_y} H904" stroke="#cbc7bd" stroke-width="1"/>
  <text x="56" y="{footer_y}" font-size="9" fill="#737169" letter-spacing="1.45" font-family="ui-sans-serif, system-ui">SOURCE-GROUNDED / EDITORIAL SVG V{_LOCAL_SVG_DESIGN_VERSION}</text>
  <text x="904" y="{footer_y}" font-size="9" fill="#737169" text-anchor="end" letter-spacing="1.1" font-family="ui-sans-serif, system-ui">ROLES, BOUNDARIES, AND ONE PRIMARY PATH</text>
</svg>
"""


def _svg_contract_deck(adapter: str, data: Mapping[str, Any]) -> str:
    if adapter == "storyboard":
        count = len(data.get("panels") or [])
        return f"{count} editorial beats · one deliberate reading path"
    if adapter == "flow":
        count = len(data.get("nodes") or [])
        return f"{count} stages · one verified source-to-result path"
    if adapter == "architecture":
        nodes = len(data.get("nodes") or [])
        edges = len(data.get("edges") or [])
        if data.get("primary_path"):
            boundaries = len(data.get("boundaries") or [])
            return (
                f"{nodes} architectural roles · one primary runtime path · "
                f"{boundaries} explicit boundar{'y' if boundaries == 1 else 'ies'}"
            )
        if edges and all(
            str(edge.get("label") or "").casefold() == "calls"
            for edge in data.get("edges") or []
        ):
            return (
                f"{edges} verified call{'s' if edges != 1 else ''} · "
                "source-backed architecture cards"
            )
        return f"{nodes} components · {edges} source-backed handoffs"
    bars = len(data.get("bars") or [])
    unit = str(data.get("unit") or "measured units").strip()
    return f"{bars} measured categories · values shown in {unit}"


def _svg_architecture_body(
    data: Mapping[str, Any],
    evidence: list[str],
    accent: str,
    paper: str,
) -> str:
    nodes = list(data["nodes"])
    edges = list(data["edges"])
    if len(edges) <= 3:
        return _svg_architecture_cards_body(nodes, edges, evidence, accent)

    columns = min(4, max(2, math.ceil(math.sqrt(len(nodes) * 1.45))))
    rows = math.ceil(len(nodes) / columns)
    gap_x = 28
    gap_y = 18
    cell_width = (824 - gap_x * (columns - 1)) / columns
    cell_height = (218 - gap_y * (rows - 1)) / rows
    positions: dict[str, tuple[float, float]] = {}
    for index, node in enumerate(nodes):
        row, column = divmod(index, columns)
        x = 68 + column * (cell_width + gap_x)
        y = 210 + row * (cell_height + gap_y)
        positions[node["id"]] = (x + 9, y + cell_height / 2)

    markup = []
    for edge in edges:
        source_x, source_y = positions[edge["source"]]
        target_x, target_y = positions[edge["target"]]
        bend = max(18.0, abs(target_x - source_x) * 0.22)
        markup.append(
            f'<path d="M{source_x:.1f} {source_y:.1f} '
            f"C{source_x + bend:.1f} {source_y:.1f} "
            f'{target_x - bend:.1f} {target_y:.1f} {target_x:.1f} {target_y:.1f}" '
            'stroke="#aaa79f" stroke-width="1" fill="none" '
            'marker-end="url(#arrow)"/>'
        )
        if edge["label"] and len(edges) <= 8:
            markup.append(
                f'<text x="{(source_x + target_x) / 2:.1f}" '
                f'y="{(source_y + target_y) / 2 - 6:.1f}" font-size="9" '
                f'text-anchor="middle" fill="{accent}" '
                'letter-spacing="0.25" '
                'font-family="ui-sans-serif, system-ui">'
                f'{html.escape(_svg_truncate(edge["label"], 16))}</text>'
            )
    for index, node in enumerate(nodes):
        row, column = divmod(index, columns)
        x = 68 + column * (cell_width + gap_x)
        y = 210 + row * (cell_height + gap_y)
        center_y = y + cell_height / 2
        endpoint = index in {0, len(nodes) - 1}
        markup.append(
            f'<circle cx="{x + 9:.1f}" cy="{center_y:.1f}" r="6" '
            f'fill="{accent if endpoint else paper}" stroke="{accent}" '
            f'stroke-width="{2 if endpoint else 1.2}"/>'
            f'<text x="{x + 24:.1f}" y="{center_y - 3:.1f}" font-size="13" '
            'fill="#242420" font-weight="650" '
            'font-family="ui-sans-serif, system-ui">'
            f'{html.escape(_svg_truncate(node["label"], 23))}</text>'
        )
        if len(nodes) <= 6 and node["detail"]:
            markup.append(
                f'<text x="{x + 24:.1f}" y="{center_y + 14:.1f}" font-size="9" '
                'fill="#77756e" font-family="ui-monospace, monospace">'
                f'{html.escape(_svg_truncate(node["detail"], 26))}</text>'
            )
    markup.append(_svg_evidence_line(evidence))
    return "\n  ".join(markup)


def _svg_architecture_cards_body(
    nodes: list[Mapping[str, Any]],
    edges: list[Mapping[str, Any]],
    evidence: list[str],
    accent: str,
) -> str:
    """Render a small relation set as editorial caller/callee cards."""

    nodes_by_id = {str(node["id"]): node for node in nodes}
    row_height = min(78.0, 228.0 / max(1, len(edges)))
    total_height = row_height * len(edges)
    start_y = 197.0 + max(0.0, (228.0 - total_height) / 2)
    markup = []
    for index, edge in enumerate(edges):
        source = nodes_by_id[str(edge["source"])]
        target = nodes_by_id[str(edge["target"])]
        y = start_y + index * row_height
        card_height = row_height - 8
        center_y = y + card_height / 2
        label = str(edge.get("label") or "hands off")
        markup.append(
            f'<rect x="68" y="{y:.1f}" width="824" height="{card_height:.1f}" '
            'fill="#fbfaf6" stroke="#cbc7bd"/>'
            f'<text x="84" y="{center_y + 6:.1f}" font-size="17" fill="{accent}" '
            'font-family="ui-monospace, SFMono-Regular, Menlo, monospace">'
            f"{index + 1:02d}</text>"
            f'<text x="130" y="{y + 17:.1f}" font-size="8" fill="#77736a" '
            'font-weight="700" letter-spacing="1.25" '
            'font-family="ui-sans-serif, system-ui">CALLER</text>'
            f'<text x="130" y="{y + 38:.1f}" font-size="13" fill="#242420" '
            'font-weight="650" font-family="ui-monospace, SFMono-Regular, Menlo, monospace">'
            f'{html.escape(_svg_truncate(source["label"], 38))}</text>'
            f'<text x="568" y="{y + 17:.1f}" font-size="8" fill="#77736a" '
            'font-weight="700" letter-spacing="1.25" '
            'font-family="ui-sans-serif, system-ui">CALLEE</text>'
            f'<text x="568" y="{y + 38:.1f}" font-size="13" fill="#242420" '
            'font-weight="650" font-family="ui-monospace, SFMono-Regular, Menlo, monospace">'
            f'{html.escape(_svg_truncate(target["label"], 38))}</text>'
            f'<text x="480" y="{y + 18:.1f}" font-size="8" fill="{accent}" '
            'text-anchor="middle" font-weight="700" letter-spacing="0.8" '
            'font-family="ui-sans-serif, system-ui">'
            f"{html.escape(_svg_truncate(label.upper(), 14))}</text>"
            f'<path d="M424 {y + 37:.1f} H536" stroke="{accent}" '
            'stroke-width="1.2" fill="none" marker-end="url(#arrow)"/>'
        )
        if source.get("detail"):
            markup.append(
                f'<text x="130" y="{y + 54:.1f}" font-size="8" fill="#77756e" '
                'font-family="ui-monospace, SFMono-Regular, Menlo, monospace">'
                f'{html.escape(_svg_truncate(source["detail"], 35))}</text>'
            )
        if target.get("detail"):
            markup.append(
                f'<text x="568" y="{y + 54:.1f}" font-size="8" fill="#77756e" '
                'font-family="ui-monospace, SFMono-Regular, Menlo, monospace">'
                f'{html.escape(_svg_truncate(target["detail"], 35))}</text>'
            )
    markup.append(_svg_evidence_line(evidence))
    return "\n  ".join(markup)


def _svg_flow_body(
    data: Mapping[str, Any],
    evidence: list[str],
    accent: str,
    paper: str,
) -> str:
    nodes_by_id = {node["id"]: node for node in data["nodes"]}
    edges_by_source = {edge["source"]: edge for edge in data["edges"]}
    incoming = {node_id: 0 for node_id in nodes_by_id}
    for edge in data["edges"]:
        incoming[edge["target"]] += 1
    cursor = next(node_id for node_id, count in incoming.items() if count == 0)
    ordered_nodes = []
    ordered_edges = []
    while True:
        ordered_nodes.append(nodes_by_id[cursor])
        edge = edges_by_source.get(cursor)
        if edge is None:
            break
        ordered_edges.append(edge)
        cursor = edge["target"]

    del paper
    left = 76
    right = 884
    step = (right - left) / max(1, len(ordered_nodes) - 1)
    markup = []
    for index, edge in enumerate(ordered_edges):
        start = left + index * step + 9
        end = left + (index + 1) * step - 9
        markup.append(
            f'<path d="M{start:.1f} 278 L{end:.1f} 278" stroke="#aaa79f" '
            'stroke-width="1.2" fill="none" marker-end="url(#arrow)"/>'
        )
        if edge["label"]:
            markup.append(
                f'<text x="{(start + end) / 2:.1f}" y="261" font-size="9" '
                f'text-anchor="middle" fill="{accent}" letter-spacing="0.2" '
                'font-family="ui-sans-serif, system-ui">'
                f'{html.escape(_svg_truncate(edge["label"], 13))}</text>'
            )
    for index, node in enumerate(ordered_nodes):
        x = left + index * step
        terminal = index == len(ordered_nodes) - 1
        markup.append(
            f'<text x="{x:.1f}" y="235" font-size="38" fill="#d6d2c8" '
            'text-anchor="middle" font-family="Georgia, Times New Roman, serif">'
            f"{index + 1:02d}</text>"
            f'<circle cx="{x:.1f}" cy="278" r="{9 if terminal else 6}" '
            f'fill="{accent if terminal else "#fbfaf6"}" stroke="{accent}" '
            'stroke-width="2"/>'
            f'<text x="{x:.1f}" y="321" font-size="12" fill="#242420" '
            'text-anchor="middle" font-weight="680" '
            'font-family="ui-sans-serif, system-ui">'
            f'{html.escape(_svg_truncate(node["label"], 16))}</text>'
        )
        if node["detail"]:
            markup.append(
                f'<text x="{x:.1f}" y="341" font-size="8.5" fill="#77756e" '
                'text-anchor="middle" font-family="ui-monospace, monospace">'
                f'{html.escape(_svg_truncate(node["detail"], 18))}</text>'
            )
    markup.append(_svg_evidence_line(evidence))
    return "\n  ".join(markup)


def _svg_storyboard_contract_body(
    data: Mapping[str, Any],
    evidence: list[str],
    accent: str,
    paper: str,
) -> str:
    panels = list(data["panels"])
    columns = min(4, len(panels))
    rows = math.ceil(len(panels) / columns)
    cell_width = 824 / columns
    row_height = 218 / rows
    positions: list[tuple[float, float]] = []
    for index in range(len(panels)):
        row, column = divmod(index, columns)
        positions.append(
            (
                68 + column * cell_width + cell_width / 2,
                244 + row * row_height,
            )
        )
    markup = []
    for index in range(len(positions) - 1):
        x, y = positions[index]
        next_x, next_y = positions[index + 1]
        markup.append(
            f'<path d="M{x + 8:.1f} {y:.1f} L{next_x - 8:.1f} {next_y:.1f}" '
            'stroke="#aaa79f" stroke-width="1.2" fill="none" '
            'marker-end="url(#arrow)"/>'
        )
    for index, (panel, (x, route_y)) in enumerate(zip(panels, positions, strict=True)):
        role = str(panel["role"] or "beat").upper()
        terminal = index == len(panels) - 1
        number_y = route_y - (35 if rows == 1 else 27)
        title_y = route_y + 40
        title_width = max(120, int(cell_width - 28))
        markup.append(
            f'<text x="{x:.1f}" y="{number_y:.1f}" font-size="{38 if rows == 1 else 27}" '
            'fill="#d6d2c8" text-anchor="middle" '
            'font-family="Georgia, Times New Roman, serif">'
            f"{index + 1:02d}</text>"
            f'<circle cx="{x:.1f}" cy="{route_y:.1f}" r="{9 if terminal else 6}" '
            f'fill="{accent if terminal else paper}" stroke="{accent}" '
            'stroke-width="2"/>'
            f'<text x="{x:.1f}" y="{route_y + 20:.1f}" font-size="8.5" '
            f'fill="{accent}" text-anchor="middle" font-weight="750" '
            'letter-spacing="1.25" font-family="ui-sans-serif, system-ui">'
            f"{html.escape(_svg_truncate(role, 16))}</text>"
        )
        markup.append(
            _svg_multiline_text(
                str(panel["title"]),
                x=int(x - title_width / 2),
                y=int(title_y),
                width=title_width,
                max_lines=2 if rows == 1 else 1,
                font_size=13 if rows == 1 else 11,
                color="#242420",
                font_weight=680,
            )
        )
        if rows == 1 and panel["detail"]:
            markup.append(
                _svg_multiline_text(
                    str(panel["detail"]),
                    x=int(x - title_width / 2),
                    y=int(title_y + 47),
                    width=title_width,
                    max_lines=2,
                    font_size=9,
                    color="#77756e",
                )
            )
    markup.append(_svg_evidence_line(evidence))
    return "\n  ".join(markup)


def _svg_bar_chart_body(
    data: Mapping[str, Any],
    evidence: list[str],
    accent: str,
    paper: str,
) -> str:
    del paper
    bars = list(data["bars"])
    maximum = max((bar["value"] for bar in bars), default=0.0) or 1.0
    plot_x = 202
    plot_y = 205
    plot_width = 624
    plot_height = 226
    row_height = plot_height / len(bars)
    bar_height = min(20.0, max(8.0, row_height * 0.42))
    markup = []
    for index, bar in enumerate(bars):
        width = max(2.0, (bar["value"] / maximum) * plot_width)
        center_y = plot_y + index * row_height + row_height / 2
        y = center_y - bar_height / 2
        value = f'{bar["value"]:g}{(" " + data["unit"]) if data["unit"] else ""}'
        fill = accent if bar["value"] == maximum else "#464641"
        markup.append(
            f'<text x="184" y="{center_y + 4:.1f}" font-size="10" '
            'text-anchor="end" fill="#55544f" '
            'font-family="ui-sans-serif, system-ui">'
            f'{html.escape(_svg_truncate(bar["label"], 18))}</text>'
            f'<path d="M{plot_x} {center_y:.1f} H{plot_x + plot_width}" '
            'stroke="#ddd9cf" stroke-width="1"/>'
            f'<rect x="{plot_x}" y="{y:.1f}" width="{width:.1f}" '
            f'height="{bar_height:.1f}" fill="{fill}"/>'
            f'<text x="{min(882.0, plot_x + width + 10):.1f}" '
            f'y="{center_y + 4:.1f}" font-size="10" fill="#242420" '
            f'font-weight="700" font-family="ui-sans-serif, system-ui">{html.escape(value)}</text>'
        )
    markup.append(_svg_evidence_line(evidence))
    return "\n  ".join(markup)


def _svg_evidence_line(evidence: list[str]) -> str:
    label = "SOURCES  " + " / ".join(evidence[:6])
    if len(evidence) > 6:
        label += f" / +{len(evidence) - 6}"
    return (
        '<text x="56" y="466" font-size="9" fill="#737169" '
        'letter-spacing="0.65" '
        'font-family="ui-monospace, SFMono-Regular, Menlo, monospace">'
        f"{html.escape(_svg_truncate(label, 126))}</text>"
    )


def _svg_truncate(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


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
    font_weight: int | None = None,
) -> str:
    # System sans glyphs average a little over half an em; using a character
    # count based on ``font_size // 2`` overfilled bold editorial labels.
    max_chars = max(12, int(width / max(1.0, font_size * 0.58)))
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
    weight = f' font-weight="{font_weight}"' if font_weight is not None else ""
    return "\n  ".join(
        f'<text x="{x}" y="{y + index * (font_size + 7)}" font-size="{font_size}" '
        f'fill="{color}"{weight} font-family="ui-sans-serif, system-ui">'
        f"{html.escape(line)}</text>"
        for index, line in enumerate(lines)
    )


def _svg_shorten(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return "…" + value[-(limit - 1) :]


__all__ = [
    "DeterministicSvgMediaGenerator",
    "OpenAICompatibleImageGenerator",
    "WikiMediaAsset",
    "image_generator_from_config",
    "materialize_deterministic_svg_slots",
    "materialize_media_slots",
    "read_generated_media_asset",
]
