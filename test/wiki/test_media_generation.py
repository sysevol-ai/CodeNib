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


def _architecture_contract(
    *,
    evidence: str = "src/runtime.py",
    first: str = "Request router",
    second: str = "Worker queue",
) -> dict:
    return {
        "schema_version": 1,
        "adapter": "architecture",
        "provenance": "deterministic-index",
        "evidence": [evidence],
        "data": {
            "nodes": [
                {"id": "router", "label": first, "evidence": [evidence]},
                {"id": "worker", "label": second, "evidence": [evidence]},
            ],
            "edges": [
                {
                    "source": "router",
                    "target": "worker",
                    "label": "dispatches",
                    "evidence": [],
                }
            ],
        },
    }


def _semantic_architecture_contract() -> dict:
    return {
        "schema_version": 1,
        "adapter": "architecture",
        "provenance": "architecture-plan",
        "evidence": ["E1", "E2", "E3", "E4", "R1", "R2", "R3"],
        "data": {
            "nodes": [
                {
                    "id": "entry",
                    "label": "Entry surfaces",
                    "detail": "Accept questions from readers.",
                    "layer": "interface",
                    "kind": "frontend",
                    "evidence": ["E1"],
                },
                {
                    "id": "coordination",
                    "label": "Analysis coordinator",
                    "detail": "Plans grounded repository work.",
                    "layer": "coordination",
                    "kind": "backend",
                    "evidence": ["E2"],
                },
                {
                    "id": "execution",
                    "label": "Source analysis",
                    "detail": "Retrieves and inspects evidence.",
                    "layer": "execution",
                    "kind": "backend",
                    "evidence": ["E3"],
                },
                {
                    "id": "result",
                    "label": "Grounded result",
                    "detail": "Returns source-linked findings.",
                    "layer": "data",
                    "kind": "database",
                    "evidence": ["E4"],
                },
            ],
            "edges": [
                {
                    "source": "entry",
                    "target": "coordination",
                    "label": "frames the question",
                    "evidence": ["R1"],
                },
                {
                    "source": "coordination",
                    "target": "execution",
                    "label": "dispatches grounded work",
                    "evidence": ["R2"],
                },
                {
                    "source": "execution",
                    "target": "result",
                    "label": "returns cited findings",
                    "evidence": ["R3"],
                },
            ],
            "primary_path": ["entry", "coordination", "execution", "result"],
            "boundaries": [
                {
                    "id": "execution_boundary",
                    "label": "Execution boundary",
                    "detail": "Planning remains separate from source execution.",
                    "members": ["coordination", "execution"],
                    "evidence": ["E2", "E3"],
                }
            ],
        },
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
            "render_contract": _architecture_contract(evidence="src/app.py"),
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
    assert "Validated render contract" in body["prompt"]
    assert "Request router" in body["prompt"]
    assert asset["metadata"]["adapter"] == "architecture"


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
            "id": "overview-diagram",
            "kind": "diagram",
            "title": "Overview architecture",
            "purpose": "Explain the repo visually.",
            "prompt": "Render the supplied contract.",
            "source_citations": ["src/runtime.py"],
            "render_contract": _semantic_architecture_contract(),
        },
        output_dir=tmp_path,
        asset_base_path="api/repos/demo/wiki-media/overview",
    )

    assert asset["uri"] == "api/repos/demo/wiki-media/overview/overview-diagram.svg"
    assert asset["mime_type"] == "image/svg+xml"
    assert asset["model"] == "local/svg"
    assert asset["metadata"]["adapter"] == "architecture"
    assert asset["metadata"]["design_system"] == "editorial-svg-v5"
    svg = (tmp_path / "overview-diagram.svg").read_text(encoding="utf-8")
    assert svg.startswith("<svg")
    assert "Entry surfaces" in svg
    assert "Analysis coordinator" in svg
    assert "DISPATCHES GROUNDED WORK" in svg
    assert "Execution boundary" in svg
    assert "PRIMARY RUNTIME PATH" in svg
    assert "CALLER" not in svg
    assert "CALLEE" not in svg
    assert "Source</text>" not in svg
    assert "Facts</text>" not in svg
    assert "linearGradient" not in svg
    assert "feDropShadow" not in svg
    assert "prompt sha256" not in svg
    assert "SOURCE-GROUNDED / EDITORIAL SVG V5" in svg


def test_deterministic_svg_escapes_untrusted_slot_text_once(tmp_path):
    DeterministicSvgMediaGenerator().generate(
        {
            "id": "unsafe-diagram",
            "kind": "diagram",
            "title": 'Unsafe "title"',
            "purpose": "Explain <script>& behavior.",
            "source_citations": ["<source>.py"],
            "render_contract": _architecture_contract(
                evidence="<source>.py",
                first="<script> router",
            ),
        },
        output_dir=tmp_path,
    )

    svg = (tmp_path / "unsafe-diagram.svg").read_text(encoding="utf-8")
    assert "<script>" not in svg
    assert "Unsafe &quot;title&quot;" in svg
    assert "Explain &lt;script&gt;&amp; behavior." not in svg
    assert "&amp;lt;script" not in svg
    assert "&lt;script&gt; router" in svg


def test_local_svg_skips_untyped_concept_slots_but_renders_typed_slots(tmp_path):
    page = {
        "citations": [{"file": "src/runtime.py"}],
        "media_slots": [
            {
                "id": "typed",
                "kind": "diagram",
                "render_contract": _architecture_contract(),
            },
            {"id": "concept", "kind": "image", "prompt": "Imagine a concept."},
        ],
    }

    materialized = materialize_media_slots(
        page,
        generator=DeterministicSvgMediaGenerator(),
        output_dir=tmp_path,
    )

    assert "asset" in materialized["media_slots"][0]
    assert "asset" not in materialized["media_slots"][1]
    assert materialized["visual_quality"] == {
        "valid": True,
        "required": False,
        "publication_ready": True,
        "errors": [],
        "typed_slots": 1,
        "valid_typed_slots": 1,
        "materialized_typed_slots": 1,
        "visible_assets": 1,
        "repository_assets": 0,
        "grounded_visuals": 1,
        "adapters": ["architecture"],
        "contracts": [
            {
                "valid": True,
                "adapter": "architecture",
                "errors": [],
                "element_count": 3,
                "evidence_count": 1,
                "slot_id": "typed",
                "materialized": True,
            }
        ],
    }


def test_local_svg_rejects_untyped_direct_generation(tmp_path):
    with pytest.raises(ValueError, match="typed render contract"):
        DeterministicSvgMediaGenerator().generate(
            {"id": "concept", "kind": "image", "prompt": "Draw it."},
            output_dir=tmp_path,
        )


def test_repository_lead_suppresses_a_duplicate_generated_lead(tmp_path):
    page = {
        "id": "overview",
        "citations": [{"file": "src/runtime.py"}],
        "media_slots": [
            {
                "id": "repository-visual",
                "kind": "image",
                "placement": "lead",
                "asset": {
                    "uri": "assets/architecture.png",
                    "provider": "repository",
                },
            },
            {
                "id": "generated-structure",
                "kind": "diagram",
                "placement": "lead",
                "render_contract": _architecture_contract(),
            },
        ],
    }

    materialized = materialize_media_slots(
        page,
        generator=DeterministicSvgMediaGenerator(),
        output_dir=tmp_path,
    )

    assert "asset" in materialized["media_slots"][0]
    assert "asset" not in materialized["media_slots"][1]
    assert materialized["visual_quality"]["publication_ready"] is True
    assert materialized["visual_quality"]["repository_assets"] == 1
    assert materialized["visual_quality"]["materialized_typed_slots"] == 0


def test_invalid_contract_is_rejected_before_provider_call(tmp_path):
    calls = []
    generator = OpenAICompatibleImageGenerator(
        model="image-model",
        api_base="https://api.example/v1",
        urlopen=lambda _request, timeout: calls.append(timeout),
    )
    page = {
        "citations": [{"file": "src/allowed.py"}],
        "media_slots": [
            {
                "id": "invalid",
                "kind": "diagram",
                "render_contract": _architecture_contract(
                    evidence="src/not-on-page.py"
                ),
            }
        ],
    }

    materialized = materialize_media_slots(
        page,
        generator=generator,
        output_dir=tmp_path,
    )

    assert calls == []
    assert materialized["media_slots"] == []
    assert materialized["visual_quality"]["valid"] is False
    assert materialized["visual_quality"]["rejected_slots"] == ["invalid"]
    assert (
        "src/not-on-page.py"
        in materialized["visual_quality"]["contracts"][0]["errors"][0]
    )


def test_local_svg_renders_chart_values_from_contract(tmp_path):
    asset = DeterministicSvgMediaGenerator().generate(
        {
            "id": "language-chart",
            "kind": "chart",
            "title": "Indexed files by language",
            "render_contract": {
                "schema_version": 1,
                "adapter": "bar-chart",
                "provenance": "metrics",
                "evidence": ["E1", "E2"],
                "data": {
                    "unit": "files",
                    "bars": [
                        {"label": "Python", "value": 12, "evidence": ["E1"]},
                        {"label": "Rust", "value": 4, "evidence": ["E2"]},
                    ],
                },
            },
        },
        output_dir=tmp_path,
    )

    svg = (tmp_path / "language-chart.svg").read_text(encoding="utf-8")
    assert asset["metadata"]["adapter"] == "bar-chart"
    assert "Python" in svg
    assert "12 files" in svg
    assert "Rust" in svg
    assert "4 files" in svg


def test_local_svg_renders_story_as_an_editorial_path(tmp_path):
    DeterministicSvgMediaGenerator().generate(
        {
            "id": "overview-reading-path",
            "kind": "storyboard",
            "title": "Overview: the reading path",
            "render_contract": {
                "schema_version": 1,
                "adapter": "storyboard",
                "provenance": "story",
                "evidence": ["E1", "E2", "E3"],
                "data": {
                    "panels": [
                        {
                            "id": "p1",
                            "title": "Enter through the public command",
                            "detail": "A repository path starts the work",
                            "role": "entry",
                            "evidence": ["E1"],
                        },
                        {
                            "id": "p2",
                            "title": "Build the searchable view",
                            "detail": "The compiler records source units",
                            "role": "mechanism",
                            "evidence": ["E2"],
                        },
                        {
                            "id": "p3",
                            "title": "Return linked context",
                            "detail": "The server resolves the requested page",
                            "role": "outcome",
                            "evidence": ["E3"],
                        },
                    ]
                },
            },
        },
        output_dir=tmp_path,
    )

    svg = (tmp_path / "overview-reading-path.svg").read_text(encoding="utf-8")
    assert "3 editorial beats · one deliberate reading path" in svg
    assert "Enter through the public" in svg
    assert "Build the searchable view" in svg
    assert "Return linked context" in svg
    assert "ENTRY" in svg
    assert "OUTCOME" in svg
    assert "#7c3aed" not in svg


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
    # Local SVG rendering draws only typed contracts, so the slot carries one.
    slot = {
        "id": "overview-image",
        "kind": "diagram",
        "prompt": "Draw it.",
        "source_citations": ["src/app.py"],
        "render_contract": _architecture_contract(evidence="src/app.py"),
        "evidence_pack": {
            "slot_id": "overview-image",
            "kind": "diagram",
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
        "citations": [{"file": "src/app.py"}],
        "media_slots": [
            {
                "id": "overview-image",
                "kind": "diagram",
                "prompt": "Draw it.",
                "source_citations": ["src/app.py"],
                "render_contract": _architecture_contract(evidence="src/app.py"),
                "evidence_pack": {
                    "slot_id": "overview-image",
                    "kind": "diagram",
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
        "citations": [{"file": "src/app.py"}],
        "media_slots": [
            {
                "id": "overview-image",
                "kind": "diagram",
                "prompt": "Draw it.",
                "render_contract": _architecture_contract(evidence="src/app.py"),
            }
        ],
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


def test_deterministic_svg_is_well_formed_xml_with_the_data_layer_label(tmp_path):
    from xml.etree import ElementTree

    DeterministicSvgMediaGenerator().generate(
        {
            "id": "overview-diagram",
            "kind": "diagram",
            "title": "Overview architecture",
            "purpose": "Explain the repo visually.",
            "source_citations": ["src/runtime.py"],
            "render_contract": _semantic_architecture_contract(),
        },
        output_dir=tmp_path,
    )

    svg = (tmp_path / "overview-diagram.svg").read_text(encoding="utf-8")
    # The data layer label carries an ampersand; unescaped it turns the whole
    # document into an XML parse error in the browser.
    assert "DATA &amp; ARTIFACTS" in svg
    assert "DATA & ARTIFACTS" not in svg
    ElementTree.fromstring(svg)


def test_deterministic_svg_refuses_to_persist_malformed_markup(tmp_path, monkeypatch):
    import codenib.wiki.media_generation as module

    monkeypatch.setattr(module, "_svg_for_slot", lambda _slot: "<svg>DATA & X</svg>")
    with pytest.raises(ValueError, match="well-formed"):
        DeterministicSvgMediaGenerator().generate(
            {
                "id": "broken",
                "kind": "diagram",
                "title": "Broken",
                "purpose": "x",
                "source_citations": ["src/runtime.py"],
                "render_contract": _semantic_architecture_contract(),
            },
            output_dir=tmp_path,
        )
    assert not (tmp_path / "broken.svg").exists()
