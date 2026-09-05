# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
import concurrent.futures
import json
import math
import threading
import time
from types import SimpleNamespace

import pytest

import codenib.wiki.media_generation as media_generation
from codenib.wiki.media_evidence import build_media_evidence_pack
from codenib.wiki.media_generation import (
    DeterministicSvgMediaGenerator,
    GeminiInteractionsImageGenerator,
    OpenAICompatibleImageGenerator,
    image_generator_from_config,
    materialize_media_slots,
)


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def read(self, size: int = -1) -> bytes:
        payload = json.dumps(self._payload).encode("utf-8")
        return payload if size < 0 else payload[:size]


_PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"
_JPEG = b"\xff\xd8\xff" + b"test-image"


def _gemini_image_response(data: bytes = _JPEG, mime_type: str = "image/jpeg"):
    return {
        "id": "interaction-test",
        "status": "completed",
        "steps": [
            {
                "type": "model_output",
                "status": "done",
                "content": [
                    {
                        "type": "image",
                        "mime_type": mime_type,
                        "data": base64.b64encode(data).decode("ascii"),
                    }
                ],
            }
        ],
    }


def test_openai_compatible_image_generator_writes_asset(tmp_path):
    requests = []
    png = base64.b64encode(_PNG).decode("ascii")

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return _Response({"data": [{"b64_json": png}]})

    generator = OpenAICompatibleImageGenerator(
        model="openai/gpt-image-1",
        api_base="http://media.local/v1",
        api_key="secret",
        size="512x512",
        timeout=12,
        urlopen=fake_urlopen,
    )
    asset = generator.generate(
        {
            "id": "overview-structure-diagram",
            "kind": "diagram",
            "purpose": "Explain the system map.",
            "prompt": "Create a compact architecture diagram.",
            "source_citations": ["src/app.py"],
        },
        output_dir=tmp_path,
    )

    assert asset["uri"] == "assets/wiki-media/overview-structure-diagram.png"
    assert asset["mime_type"] == "image/png"
    assert asset["model"] == "openai/gpt-image-1"
    assert asset["source_citations"] == ["src/app.py"]
    assert (tmp_path / "overview-structure-diagram.png").read_bytes() == _PNG
    request, timeout = requests[0]
    assert request.full_url == "http://media.local/v1/images/generations"
    assert request.get_header("Authorization") == "Bearer secret"
    assert timeout == 12
    body = json.loads(request.data.decode("utf-8"))
    assert body["model"] == "openai/gpt-image-1"
    assert body["size"] == "512x512"
    assert "src/app.py" in body["prompt"]


def test_gemini_interactions_generator_writes_grounded_asset(tmp_path):
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return _Response(_gemini_image_response())

    generator = GeminiInteractionsImageGenerator(
        model="gemini-3.1-flash-image",
        api_key="secret",
        aspect_ratio="16:9",
        image_size="1K",
        timeout=15,
        urlopen=fake_urlopen,
    )
    asset = generator.generate(
        {
            "id": "overview-architecture",
            "kind": "diagram",
            "purpose": "Explain the Wiki request path.",
            "prompt": "Create a compact technical architecture illustration.",
            "source_citations": ["codenib/web/app.py"],
        },
        output_dir=tmp_path,
    )

    assert asset["uri"] == "assets/wiki-media/overview-architecture.jpg"
    assert asset["provider"] == "google-gemini"
    assert asset["model"] == "gemini-3.1-flash-image"
    assert asset["mime_type"] == "image/jpeg"
    assert asset["source_citations"] == ["codenib/web/app.py"]
    assert asset["metadata"] == {
        "aspect_ratio": "16:9",
        "image_size": "1K",
        "delivery": "inline",
        "stored_by_provider": False,
    }
    assert (tmp_path / "overview-architecture.jpg").read_bytes() == _JPEG

    request, timeout = requests[0]
    assert request.full_url == (
        "https://generativelanguage.googleapis.com/v1beta/interactions"
    )
    assert request.get_header("X-goog-api-key") == "secret"
    assert request.get_header("Authorization") is None
    assert timeout == 15
    body = json.loads(request.data.decode("utf-8"))
    assert body["model"] == "gemini-3.1-flash-image"
    assert body["store"] is False
    assert body["response_format"] == {
        "type": "image",
        "mime_type": "image/jpeg",
        "aspect_ratio": "16:9",
        "image_size": "1K",
        "delivery": "inline",
    }
    assert "codenib/web/app.py" in body["input"][0]["text"]


def test_gemini_generator_reuses_verified_local_asset(tmp_path):
    calls = []

    def fake_urlopen(_request, timeout):
        calls.append(timeout)
        return _Response(_gemini_image_response())

    generator = GeminiInteractionsImageGenerator(
        model="gemini-3.1-flash-image",
        api_key="secret",
        urlopen=fake_urlopen,
    )
    slot = {"id": "overview-image", "kind": "image", "prompt": "Draw it."}

    first = generator.generate(slot, output_dir=tmp_path)
    second = generator.generate(slot, output_dir=tmp_path)
    changed_format = GeminiInteractionsImageGenerator(
        model="gemini-3.1-flash-image",
        api_key="secret",
        aspect_ratio="4:3",
        urlopen=fake_urlopen,
    ).generate(slot, output_dir=tmp_path)

    assert first == second
    assert changed_format["metadata"]["aspect_ratio"] == "4:3"
    assert calls == [120.0, 120.0]
    manifest = json.loads(
        (tmp_path / "overview-image.jpg.json").read_text(encoding="utf-8")
    )
    assert len(manifest["generation_sha256"]) == 64
    assert len(manifest["content_sha256"]) == 64


def test_gemini_generator_uses_last_model_image(tmp_path):
    response = _gemini_image_response()
    response["steps"].insert(
        0,
        {
            "type": "model_output",
            "content": [
                {
                    "type": "image",
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(_JPEG + b"first").decode("ascii"),
                }
            ],
        },
    )
    generator = GeminiInteractionsImageGenerator(
        model="gemini-3.1-flash-image",
        api_key="secret",
        urlopen=lambda _request, timeout: _Response(response),
    )

    generator.generate(
        {"id": "overview-image", "kind": "image", "prompt": "Draw it."},
        output_dir=tmp_path,
    )

    assert (tmp_path / "overview-image.jpg").read_bytes() == _JPEG


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({"steps": []}, "no model image output"),
        (
            _gemini_image_response(mime_type="image/png"),
            "MIME type does not match",
        ),
        (
            _gemini_image_response(data=b"not-a-jpeg"),
            "not a valid image/jpeg",
        ),
    ],
)
def test_gemini_generator_rejects_invalid_image_output(tmp_path, response, message):
    generator = GeminiInteractionsImageGenerator(
        model="gemini-3.1-flash-image",
        api_key="secret",
        urlopen=lambda _request, timeout: _Response(response),
    )

    with pytest.raises(ValueError, match=message):
        generator.generate(
            {"id": "overview-image", "kind": "image", "prompt": "Draw it."},
            output_dir=tmp_path,
        )


def test_gemini_generator_rejects_invalid_base64(tmp_path):
    response = _gemini_image_response()
    response["steps"][0]["content"][0]["data"] = "not+valid=base64!"
    generator = GeminiInteractionsImageGenerator(
        model="gemini-3.1-flash-image",
        api_key="secret",
        urlopen=lambda _request, timeout: _Response(response),
    )

    with pytest.raises(ValueError, match="invalid base64"):
        generator.generate(
            {"id": "overview-image", "kind": "image", "prompt": "Draw it."},
            output_dir=tmp_path,
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"api_key": ""}, "API key is required"),
        ({"api_base": "http://example.test/v1beta"}, "must use HTTPS"),
        ({"aspect_ratio": "7:5"}, "aspect_ratio must be one of"),
        ({"image_size": "8K"}, "image_size must be one of"),
        ({"mime_type": "image/webp"}, "mime_type must be one of"),
    ],
)
def test_gemini_generator_rejects_unsafe_configuration(overrides, message):
    arguments = {
        "model": "gemini-3.1-flash-image",
        "api_key": "secret",
    }
    arguments.update(overrides)

    with pytest.raises(ValueError, match=message):
        GeminiInteractionsImageGenerator(**arguments)


def test_image_generator_factory_routes_gemini_provider():
    config = SimpleNamespace(
        wiki_media_generation_enabled=True,
        wiki_media_model="gemini-3.1-flash-image",
        wiki_media_api_base=None,
        wiki_media_api_key="secret",
        wiki_media_options={
            "provider": "gemini",
            "aspect_ratio": "4:3",
            "image_size": "2K",
            "mime_type": "image/jpeg",
            "timeout": 30,
        },
    )

    generator = image_generator_from_config(config)

    assert isinstance(generator, GeminiInteractionsImageGenerator)
    assert generator.endpoint.endswith("/v1beta/interactions")
    assert generator.aspect_ratio == "4:3"
    assert generator.image_size == "2K"


def test_materialize_media_slots_skips_unsupported_video_slots(tmp_path):
    calls = []

    def fake_urlopen(_request, timeout):
        calls.append(True)
        encoded = base64.b64encode(_PNG).decode("ascii")
        return _Response({"data": [{"b64_json": encoded}]})

    generator = OpenAICompatibleImageGenerator(
        model="openai/gpt-image-1",
        api_base="http://media.local/v1",
        urlopen=fake_urlopen,
    )
    page = {
        "id": "overview",
        "media_slots": [
            {"id": "overview-image", "kind": "image", "prompt": "Draw it."},
            {"id": "overview-video", "kind": "video", "prompt": "Animate it."},
        ],
    }

    materialized = materialize_media_slots(
        page, generator=generator, output_dir=tmp_path
    )

    assert len(calls) == 1
    assert "asset" in materialized["media_slots"][0]
    assert "asset" not in materialized["media_slots"][1]


def test_materialize_media_slots_uses_server_side_evidence_without_exposing_it(
    tmp_path,
):
    requests = []
    png = base64.b64encode(_PNG).decode("ascii")

    def fake_urlopen(request, timeout):
        requests.append(request)
        return _Response({"data": [{"b64_json": png}]})

    generator = OpenAICompatibleImageGenerator(
        model="openai/gpt-image-1",
        api_base="https://api.example/v1",
        urlopen=fake_urlopen,
    )
    page = {
        "id": "overview",
        "title": "Overview",
        "content": "This page explains wiki media generation.",
        "media_slots": [
            {
                "id": "overview-image",
                "kind": "image",
                "prompt": "Draw a grounded concept.",
                "source_citations": ["src/app.py"],
            }
        ],
    }

    def evidence_builder(slot):
        return build_media_evidence_pack(
            slot,
            page_id=page["id"],
            page_title=page["title"],
            page_markdown=page["content"],
            citations=[
                {
                    "file": "src/app.py",
                    "symbol": "build_page",
                    "snippet": "secret server-side source snippet",
                }
            ],
            relations=[{"from": "build_page", "to": "render_media", "kind": "calls"}],
        )

    materialized = materialize_media_slots(
        page,
        generator=generator,
        output_dir=tmp_path,
        evidence_builder=evidence_builder,
    )

    body = json.loads(requests[0].data.decode("utf-8"))
    assert "secret server-side source snippet" in body["prompt"]
    slot = materialized["media_slots"][0]
    assert "evidence_pack" not in slot
    assert "secret server-side source snippet" not in json.dumps(slot)
    assert "evidence_pack_sha256" in slot["asset"]["metadata"]


def test_deterministic_svg_generator_writes_visible_asset(tmp_path):
    generator = DeterministicSvgMediaGenerator()

    asset = generator.generate(
        {
            "id": "overview-image",
            "kind": "image",
            "title": "Overview image",
            "purpose": "Explain the repo visually.",
            "prompt": "Draw a source-grounded concept.",
            "source_citations": ["src/runtime.py"],
        },
        output_dir=tmp_path,
        asset_base_path="api/repos/demo/wiki-media/overview",
    )

    assert asset["uri"] == "api/repos/demo/wiki-media/overview/overview-image.svg"
    assert asset["mime_type"] == "image/svg+xml"
    assert asset["model"] == "local/svg"
    assert (
        (tmp_path / "overview-image.svg").read_text(encoding="utf-8").startswith("<svg")
    )


def test_deterministic_svg_escapes_untrusted_slot_text_once(tmp_path):
    DeterministicSvgMediaGenerator().generate(
        {
            "id": "unsafe-image",
            "kind": "image",
            "title": 'Unsafe "title"',
            "purpose": "Explain <script>& behavior.",
            "source_citations": ["<source>.py"],
        },
        output_dir=tmp_path,
    )

    svg = (tmp_path / "unsafe-image.svg").read_text(encoding="utf-8")
    assert "<script>" not in svg
    assert "&lt;script&gt;&amp; behavior." in svg
    assert "&amp;lt;script" not in svg
    assert "&lt;source&gt;.py" in svg


def test_media_generation_reuses_cached_asset(tmp_path):
    calls = []
    png = base64.b64encode(_PNG).decode("ascii")

    def fake_urlopen(_request, timeout):
        calls.append(True)
        return _Response({"data": [{"b64_json": png}]})

    generator = OpenAICompatibleImageGenerator(
        model="openai/gpt-image-1",
        api_base="http://media.local/v1",
        urlopen=fake_urlopen,
    )
    slot = {
        "id": "overview-image",
        "kind": "image",
        "prompt": "Draw it.",
    }

    first = generator.generate(slot, output_dir=tmp_path)
    second = generator.generate(slot, output_dir=tmp_path)
    generator.generate({**slot, "title": "Changed title"}, output_dir=tmp_path)

    assert first == second
    assert len(calls) == 2


def test_asset_prompt_redacts_evidence_pack_contents(tmp_path):
    generator = DeterministicSvgMediaGenerator()
    slot = {
        "id": "overview-image",
        "kind": "image",
        "prompt": "Draw it.",
        "source_citations": ["src/app.py"],
        "evidence_pack": {
            "slot_id": "overview-image",
            "kind": "image",
            "sources": [
                {"file": "src/app.py", "snippet": "private code snippet"},
            ],
        },
    }

    asset = generator.generate(slot, output_dir=tmp_path)

    assert "private code snippet" not in asset["prompt"]
    assert asset["metadata"]["evidence_pack_sha256"]


def test_materialize_media_slots_redacts_input_evidence_pack(tmp_path):
    page = {
        "id": "overview",
        "media_slots": [
            {
                "id": "overview-image",
                "kind": "image",
                "prompt": "Draw it.",
                "evidence_pack": {
                    "slot_id": "overview-image",
                    "kind": "image",
                    "sources": [
                        {"file": "src/app.py", "snippet": "server-only source"}
                    ],
                },
            }
        ],
    }

    materialized = materialize_media_slots(
        page,
        generator=DeterministicSvgMediaGenerator(),
        output_dir=tmp_path,
    )

    slot = materialized["media_slots"][0]
    assert "evidence_pack" not in slot
    assert "server-only source" not in json.dumps(materialized)
    assert slot["asset"]["metadata"]["evidence_pack_sha256"]


def test_materialize_media_slots_rejects_non_mapping_evidence(tmp_path):
    page = {
        "media_slots": [{"id": "overview-image", "kind": "image", "prompt": "Draw it."}]
    }

    with pytest.raises(ValueError, match="must return a mapping or None"):
        materialize_media_slots(
            page,
            generator=DeterministicSvgMediaGenerator(),
            output_dir=tmp_path,
            evidence_builder=lambda _slot: ["not", "a", "mapping"],
        )


def test_generation_normalizes_caller_supplied_evidence_pack(tmp_path):
    requests = []
    png = base64.b64encode(_PNG).decode("ascii")

    def fake_urlopen(request, timeout):
        requests.append(request)
        assert timeout == 120.0
        return _Response({"data": [{"b64_json": png}]})

    generator = OpenAICompatibleImageGenerator(
        model="image-model",
        api_base="https://api.example/v1",
        urlopen=fake_urlopen,
    )
    generator.generate(
        {
            "id": "overview-image",
            "kind": "image",
            "prompt": "Draw it.",
            "evidence_pack": {
                "slot_id": "overview-image",
                "kind": "image",
                "unknown": "must not reach the provider",
                "sources": [
                    {"file": "../secret.py", "snippet": "server-only secret"},
                    {
                        "file": "src/app.py",
                        "node_name": "create_app",
                        "content": "safe source",
                    },
                ],
            },
        },
        output_dir=tmp_path,
    )

    prompt = json.loads(requests[0].data.decode("utf-8"))["prompt"]
    assert "safe source" in prompt
    assert "create_app" in prompt
    assert "server-only secret" not in prompt
    assert "../secret.py" not in prompt
    assert "must not reach the provider" not in prompt


def test_evidence_serialization_rejects_oversized_noncanonical_payload():
    with pytest.raises(ValueError, match="evidence pack exceeds"):
        media_generation._serialized_evidence_pack({"raw": "x" * (25 * 1024)})


def test_hosted_image_url_is_validated_and_cached(tmp_path):
    calls = []

    def fake_urlopen(_request, timeout):
        calls.append(timeout)
        return _Response({"data": [{"url": "https://media.example/asset.png"}]})

    generator = OpenAICompatibleImageGenerator(
        model="image-model",
        api_base="https://api.example/v1",
        urlopen=fake_urlopen,
    )
    slot = {"id": "overview-image", "kind": "image", "prompt": "Draw it."}

    first = generator.generate(slot, output_dir=tmp_path)
    second = generator.generate(slot, output_dir=tmp_path)

    assert first == second
    assert first["uri"] == "https://media.example/asset.png"
    assert calls == [120.0]
    assert not (tmp_path / "overview-image.png").exists()


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/asset.png",
        "javascript:alert(1)",
        "https://user:secret@media.example/asset.png",
        "https://media.example/bad path.png",
        "//media.example/asset.png",
    ],
)
def test_hosted_image_url_rejects_unsafe_schemes(tmp_path, url):
    generator = OpenAICompatibleImageGenerator(
        model="image-model",
        api_base="https://api.example/v1",
        urlopen=lambda _request, timeout: _Response({"data": [{"url": url}]}),
    )

    with pytest.raises(ValueError, match="generated image URL"):
        generator.generate(
            {"id": "overview-image", "kind": "image", "prompt": "Draw it."},
            output_dir=tmp_path,
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"api_base": "file:///tmp"}, "api_base"),
        ({"api_base": "https://user:secret@example.test/v1"}, "credentials"),
        ({"size": "0x1024"}, "WIDTHxHEIGHT"),
        ({"size": "8192x1024"}, "must not exceed"),
        ({"api_key": "secret\nheader"}, "API key"),
        ({"timeout": True}, "positive number"),
        ({"timeout": math.nan}, "between 0"),
        ({"timeout": 601}, "between 0"),
    ],
)
def test_image_generator_rejects_unsafe_configuration(overrides, message):
    arguments = {"model": "image-model", "api_base": "https://api.example/v1"}
    arguments.update(overrides)

    with pytest.raises(ValueError, match=message):
        OpenAICompatibleImageGenerator(**arguments)


def test_image_response_and_decoded_payload_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(media_generation, "_MAX_IMAGE_RESPONSE_BYTES", 32)

    class OversizedResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def read(self, size):
            assert size == 33
            return b"x" * size

    generator = OpenAICompatibleImageGenerator(
        model="image-model",
        api_base="https://api.example/v1",
        urlopen=lambda _request, timeout: OversizedResponse(),
    )
    slot = {"id": "overview-image", "kind": "image", "prompt": "Draw it."}

    with pytest.raises(ValueError, match="response exceeds"):
        generator.generate(slot, output_dir=tmp_path)

    monkeypatch.setattr(media_generation, "_MAX_IMAGE_RESPONSE_BYTES", 1024)
    monkeypatch.setattr(media_generation, "_MAX_IMAGE_BYTES", 8)
    generator = OpenAICompatibleImageGenerator(
        model="image-model",
        api_base="https://api.example/v1",
        urlopen=lambda _request, timeout: _Response(
            {"data": [{"b64_json": base64.b64encode(_PNG).decode("ascii")}]}
        ),
    )
    with pytest.raises(ValueError, match="image exceeds"):
        generator.generate(slot, output_dir=tmp_path)


def test_image_generation_requires_output_directory_before_provider_call():
    calls = []
    generator = OpenAICompatibleImageGenerator(
        model="image-model",
        api_base="https://api.example/v1",
        urlopen=lambda _request, timeout: calls.append(timeout),
    )

    with pytest.raises(ValueError, match="output_dir"):
        generator.generate(
            {"id": "overview-image", "kind": "image", "prompt": "Draw it."}
        )

    assert calls == []


def test_image_generator_rejects_non_png_b64_payload(tmp_path):
    generator = OpenAICompatibleImageGenerator(
        model="image-model",
        api_base="https://api.example/v1",
        urlopen=lambda _request, timeout: _Response(
            {"data": [{"b64_json": base64.b64encode(b"not-an-image").decode("ascii")}]}
        ),
    )

    with pytest.raises(ValueError, match="not a PNG"):
        generator.generate(
            {"id": "overview-image", "kind": "image", "prompt": "Draw it."},
            output_dir=tmp_path,
        )


def test_cached_asset_content_mutation_forces_regeneration(tmp_path):
    calls = []

    def fake_urlopen(_request, timeout):
        calls.append(timeout)
        return _Response(
            {"data": [{"b64_json": base64.b64encode(_PNG).decode("ascii")}]}
        )

    generator = OpenAICompatibleImageGenerator(
        model="image-model",
        api_base="https://api.example/v1",
        urlopen=fake_urlopen,
    )
    slot = {"id": "overview-image", "kind": "image", "prompt": "Draw it."}
    generator.generate(slot, output_dir=tmp_path)
    (tmp_path / "overview-image.png").write_bytes(_PNG + b"tampered")

    generator.generate(slot, output_dir=tmp_path)

    assert calls == [120.0, 120.0]
    assert (tmp_path / "overview-image.png").read_bytes() == _PNG
    assert not list(tmp_path.glob("*.tmp"))


def test_concurrent_generation_uses_one_provider_call(tmp_path):
    calls = []
    start = threading.Barrier(2)

    def fake_urlopen(_request, timeout):
        calls.append(timeout)
        time.sleep(0.05)
        return _Response(
            {"data": [{"b64_json": base64.b64encode(_PNG).decode("ascii")}]}
        )

    generator = OpenAICompatibleImageGenerator(
        model="image-model",
        api_base="https://api.example/v1",
        urlopen=fake_urlopen,
    )
    slot = {"id": "overview-image", "kind": "image", "prompt": "Draw it."}

    def generate():
        start.wait()
        return generator.generate(slot, output_dir=tmp_path)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: generate(), range(2)))

    assert results[0] == results[1]
    assert calls == [120.0]


def test_materialize_media_slots_rejects_unbounded_slot_count(tmp_path, monkeypatch):
    monkeypatch.setattr(media_generation, "_MAX_MEDIA_SLOTS", 1)

    with pytest.raises(ValueError, match="slot count"):
        materialize_media_slots(
            {
                "media_slots": [
                    {"id": "one", "kind": "video"},
                    {"id": "two", "kind": "video"},
                ]
            },
            generator=DeterministicSvgMediaGenerator(),
            output_dir=tmp_path,
        )


def test_unsafe_slot_ids_map_to_distinct_flat_filenames():
    first = media_generation._safe_filename("a/b")
    second = media_generation._safe_filename("a b")

    assert first != second
    assert "/" not in first
    assert "/" not in second
    assert media_generation._safe_filename("CON") != "CON"
