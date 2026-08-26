# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType

import pytest

import codenib.provider_routes as provider_routes_module
from codenib.provider_routes import (
    embedding_compatibility_options,
    normalize_endpoint,
    normalize_provider,
    resolve_embedding_artifact_route,
    resolve_inference_route,
    validate_embedding_runtime_options,
)


def test_route_callback_polls_long_model_and_preserves_current_error() -> None:
    stop = KeyboardInterrupt("injected long route model stop")
    polls = 0

    def check_cancelled() -> None:
        nonlocal polls
        polls += 1
        if polls < 2:
            return
        raise stop

    with pytest.raises(KeyboardInterrupt) as caught:
        resolve_inference_route(
            operation="embeddings",
            provider="huggingface",
            model="m" * (16 * 1024 * 1024),
            dimension=4,
            environ={},
            check_cancelled=check_cancelled,
        )
    assert caught.value is stop

    polls = 0
    with pytest.raises(ValueError, match="without whitespace") as current:
        resolve_inference_route(
            operation="embeddings",
            provider="huggingface",
            model="invalid model" + "m" * (16 * 1024 * 1024),
            dimension=4,
            environ={},
            check_cancelled=check_cancelled,
        )
    assert current.value is not stop


def test_route_callback_polls_endpoint_scalar_and_keeps_current_policy() -> None:
    stop = SystemExit("injected long route endpoint stop")
    polls = 0

    def check_cancelled() -> None:
        nonlocal polls
        polls += 1
        if polls == 4:
            raise stop

    with pytest.raises(SystemExit) as caught:
        resolve_inference_route(
            operation="embeddings",
            provider="openai",
            model="test-model",
            endpoint="https://example.test/" + "x" * (16 * 1024 * 1024),
            dimension=4,
            environ={},
            check_cancelled=check_cancelled,
        )
    assert caught.value is stop

    polls = 0
    with pytest.raises(ValueError, match="path traversal") as current:
        resolve_inference_route(
            operation="embeddings",
            provider="openai",
            model="test-model",
            endpoint="https://example.test/../" + "x" * (16 * 1024 * 1024),
            dimension=4,
            environ={},
            check_cancelled=check_cancelled,
        )
    assert current.value is not stop


def test_provider_callback_attests_current_chunk_before_future_poll() -> None:
    stop = RuntimeError("injected provider future stop")
    polls = 0

    def check_cancelled() -> None:
        nonlocal polls
        polls += 1
        raise stop

    with pytest.raises(ValueError, match="invalid inference provider") as current:
        normalize_provider(
            "?" + "a" * (128 * 1024),
            check_cancelled=check_cancelled,
        )
    assert current.value is not stop
    assert polls == 0

    with pytest.raises(RuntimeError) as caught:
        normalize_provider(
            "a" * (128 * 1024),
            check_cancelled=check_cancelled,
        )
    assert caught.value is stop


@pytest.mark.parametrize(
    "value",
    [
        "ftp://example.test",
        "https://user@example.test/v1",
        "https://example.test/v1?query=value",
        "https://example.test/v1#fragment",
        "https://example.test/../v1",
        "https://example.test/v1/embeddings",
        "https://example.test\\evil/v1",
    ],
)
def test_endpoint_callback_keeps_short_current_policy(value: str) -> None:
    stop = RuntimeError("injected endpoint future stop")
    polls = 0

    def check_cancelled() -> None:
        nonlocal polls
        polls += 1
        raise stop

    with pytest.raises(ValueError) as caught:
        normalize_endpoint(value, check_cancelled=check_cancelled)
    assert caught.value is not stop
    assert polls == 0


@pytest.mark.parametrize(
    "path",
    ["../", "%2e%2e/", "safe/\x01"],
)
def test_endpoint_callback_attests_long_first_path_chunk(path: str) -> None:
    stop = RuntimeError("injected endpoint future stop")
    polls = 0

    def check_cancelled() -> None:
        nonlocal polls
        polls += 1
        raise stop

    with pytest.raises(ValueError) as caught:
        normalize_endpoint(
            "https://example.test/" + path + "x" * (128 * 1024),
            check_cancelled=check_cancelled,
        )
    assert caught.value is not stop
    assert polls == 0


@pytest.mark.parametrize(
    "authority",
    ["x:bad", "x:99999", ":80", "[x", "x\\evil"],
)
def test_endpoint_callback_attests_completed_current_authority(
    authority: str,
) -> None:
    stop = RuntimeError("injected endpoint authority stop")
    polls = 0

    def check_cancelled() -> None:
        nonlocal polls
        polls += 1
        raise stop

    with pytest.raises(ValueError) as caught:
        normalize_endpoint(
            f"https://{authority}/" + "x" * (200 * 1024),
            check_cancelled=check_cancelled,
        )
    assert caught.value is not stop
    assert polls == 0


@pytest.mark.parametrize("prefix", ["x:bad", "x:99999", ":80"])
def test_endpoint_callback_attests_invalid_long_authority_prefix(prefix: str) -> None:
    stop = RuntimeError("injected long-authority future stop")
    polls = 0

    def check_cancelled() -> None:
        nonlocal polls
        polls += 1
        raise stop

    with pytest.raises(ValueError) as caught:
        normalize_endpoint(
            "https://" + prefix + "a" * (70 * 1024) + "/",
            check_cancelled=check_cancelled,
        )
    assert caught.value is not stop
    assert polls == 0


@pytest.mark.parametrize(
    "prefix",
    ["x]", "x[", "exa\uff0fmple", "exa\uff1ample", "exa\uff20mple"],
)
def test_endpoint_callback_attests_invalid_unicode_authority_prefix(
    prefix: str,
) -> None:
    stop = RuntimeError("injected Unicode-authority future stop")
    polls = 0

    def check_cancelled() -> None:
        nonlocal polls
        polls += 1
        raise stop

    with pytest.raises(ValueError) as caught:
        normalize_endpoint(
            "https://" + prefix + "a" * (70 * 1024) + "/",
            check_cancelled=check_cancelled,
        )
    assert caught.value is not stop
    assert polls == 0


def test_endpoint_callback_retains_decoded_operation_before_slash_tail() -> None:
    value = "https://example.test/v1/%65mbeddings" + "/" * (64 * 1024)
    with pytest.raises(ValueError, match="operation path"):
        normalize_endpoint(value)
    with pytest.raises(ValueError, match="operation path"):
        normalize_endpoint(value, check_cancelled=lambda: None)

    stop = KeyboardInterrupt("injected operation-tail stop")
    polls = 0

    def check_cancelled() -> None:
        nonlocal polls
        polls += 1
        if polls == 3:
            raise stop

    with pytest.raises(ValueError, match="operation path") as caught:
        normalize_endpoint(value, check_cancelled=check_cancelled)
    assert caught.value is not stop
    assert polls == 2


def test_endpoint_callback_stops_before_future_long_path_chunk() -> None:
    stop = RuntimeError("injected endpoint future stop")

    def check_cancelled() -> None:
        raise stop

    with pytest.raises(RuntimeError) as caught:
        normalize_endpoint(
            "https://example.test/" + "x" * (128 * 1024),
            check_cancelled=check_cancelled,
        )
    assert caught.value is stop


def test_route_callback_canonical_scalar_is_chunked_and_equivalent() -> None:
    value = {"option": "\U0001f642" * (4 * 1024 * 1024)}
    stop = BaseException("injected route JSON scalar stop")

    def check_cancelled() -> None:
        raise stop

    with pytest.raises(BaseException) as caught:
        provider_routes_module._json_dumps_interruptibly(
            value,
            check_cancelled=check_cancelled,
        )
    assert caught.value is stop

    assert provider_routes_module._json_dumps_interruptibly(
        {"nested": ["\U0001f642", {"value": 1.25}]},
        check_cancelled=lambda: None,
    ) == json.dumps(
        {"nested": ["\U0001f642", {"value": 1.25}]},
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def test_route_callback_detaches_nested_mapping_subclass_and_freezes_options() -> None:
    class CompatibleMapping(dict[str, object]):
        pass

    callback_mapping = provider_routes_module._json_mapping(
        {"nested": CompatibleMapping(value=1)},
        source="test route",
        check_cancelled=lambda: None,
    )
    legacy_mapping = provider_routes_module._json_mapping(
        {"nested": CompatibleMapping(value=1)},
        source="test route",
    )
    assert callback_mapping == legacy_mapping == {"nested": {"value": 1}}

    options = {"model_kwargs": {"revision": "a" * 40}}
    callback_route = resolve_inference_route(
        operation="embeddings",
        provider="huggingface",
        model="test/model",
        dimension=4,
        compatibility_options=options,
        environ={},
        check_cancelled=lambda: None,
    )
    legacy_route = resolve_inference_route(
        operation="embeddings",
        provider="huggingface",
        model="test/model",
        dimension=4,
        compatibility_options={"model_kwargs": {"revision": "a" * 40}},
        environ={},
    )
    options["model_kwargs"]["revision"] = "b" * 40

    assert callback_route == legacy_route
    assert callback_route.compatibility_options == {
        "model_kwargs": {"revision": "a" * 40}
    }
    assert hash(callback_route) == hash(legacy_route)

    proxy_options = MappingProxyType({"model_kwargs": {"revision": "a" * 40}})
    assert (
        resolve_inference_route(
            operation="embeddings",
            provider="huggingface",
            model="test/model",
            dimension=4,
            compatibility_options=proxy_options,
            environ={},
            check_cancelled=lambda: None,
        )
        == legacy_route
    )


def test_route_callback_preserves_exact_stop_iteration() -> None:
    stop = StopIteration("injected exact route stop")

    def check_cancelled() -> None:
        raise stop

    with pytest.raises(StopIteration) as caught:
        provider_routes_module._json_dumps_interruptibly(
            {"value": "x" * (128 * 1024)},
            check_cancelled=check_cancelled,
        )
    assert caught.value is stop

    with pytest.raises(StopIteration) as route_caught:
        resolve_embedding_artifact_route(
            {
                "embedding_model": "test/model",
                "embedding_provider": "huggingface",
                "dimension": 4,
            },
            environ={},
            check_cancelled=check_cancelled,
        )
    assert route_caught.value is stop


def test_route_callback_detaches_current_key_subclass_before_future_stop() -> None:
    class HostileKey(str):
        pass

    stop = RuntimeError("injected route key stop")
    polls = 0

    def check_cancelled() -> None:
        nonlocal polls
        polls += 1
        if polls > 1:
            raise stop

    assert provider_routes_module._json_mapping(
        {HostileKey("current"): 1},
        source="test route",
        check_cancelled=check_cancelled,
    ) == {"current": 1}
    assert polls == 0

    touched_value = False

    class HostileMapping(Mapping[HostileKey, object]):
        def __getitem__(self, key: HostileKey) -> object:
            nonlocal touched_value
            touched_value = True
            raise AssertionError("route mapping touched an invalid current key")

        def __iter__(self):  # type: ignore[no-untyped-def]
            return iter((HostileKey("current"),))

        def __len__(self) -> int:
            return 1

    def stop_after_value_touch() -> None:
        if touched_value:
            raise stop

    with pytest.raises(AssertionError, match="touched an invalid current key"):
        provider_routes_module._json_mapping(
            HostileMapping(),
            source="test route",
            check_cancelled=stop_after_value_touch,
        )
    assert touched_value


def test_route_mapping_current_invalid_value_precedes_armed_stop() -> None:
    stop = RuntimeError("injected route mapping stop")
    armed = False

    class InvalidMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            nonlocal armed
            assert key == "current"
            armed = True
            return object()

        def __iter__(self):  # type: ignore[no-untyped-def]
            return iter(("current",))

        def __len__(self) -> int:
            return 1

    def check_cancelled() -> None:
        if armed:
            raise stop

    with pytest.raises(ValueError, match="JSON-compatible") as caught:
        provider_routes_module._json_mapping(
            InvalidMapping(),
            source="test route",
            check_cancelled=check_cancelled,
        )

    assert caught.value is not stop


def test_route_mapping_uses_legacy_keys_and_nested_items_protocols() -> None:
    class TopLevel(dict[str, int]):
        def keys(self):  # type: ignore[no-untyped-def]
            return ["selected"]

        def __iter__(self):  # type: ignore[no-untyped-def]
            return iter(("ignored",))

        def __getitem__(self, key: str) -> int:
            assert key == "selected"
            return 2

    class Nested(dict[str, int]):
        def items(self):  # type: ignore[no-untyped-def]
            return [("selected", 3)]

    value = TopLevel(ignored=1)
    assert provider_routes_module._json_mapping(
        value,
        source="test route",
        check_cancelled=lambda: None,
    ) == provider_routes_module._json_mapping(value, source="test route")
    nested = {"nested": Nested(ignored=1)}
    assert provider_routes_module._json_mapping(
        nested,
        source="test route",
        check_cancelled=lambda: None,
    ) == provider_routes_module._json_mapping(nested, source="test route")


def test_route_mapping_preserves_falsey_and_numeric_key_legacy_semantics() -> None:
    class FalseyOptions(dict[str, object]):
        def __bool__(self) -> bool:
            return False

    class NumericOptions(dict[object, str]):
        pass

    for value in (FalseyOptions(value=1), NumericOptions({10: "ten", 2: "two"})):
        legacy = provider_routes_module._json_mapping(value, source="test route")
        callback = provider_routes_module._json_mapping(
            value,
            source="test route",
            check_cancelled=lambda: None,
        )
        assert callback == legacy
        assert list(callback) == list(legacy)


def test_route_mapping_polls_after_lazy_key_before_future_value_read() -> None:
    stop = StopIteration("injected lazy route mapping stop")
    armed = False
    value_touched = False

    class LazyMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            nonlocal value_touched
            value_touched = True
            raise AssertionError("route mapping touched a poisoned future value")

        def __iter__(self):  # type: ignore[no-untyped-def]
            nonlocal armed
            armed = True
            yield "current"

        def __len__(self) -> int:
            return 1

    def check_cancelled() -> None:
        if armed:
            raise stop

    with pytest.raises(StopIteration) as caught:
        provider_routes_module._json_mapping(
            LazyMapping(),
            source="test route",
            check_cancelled=check_cancelled,
        )
    assert caught.value is stop
    assert not value_touched


@pytest.mark.parametrize("forbidden_key", ["api_key", "base_url"])
def test_route_policy_rejects_current_lazy_key_before_future_value(
    forbidden_key: str,
) -> None:
    stop = StopIteration("injected lazy forbidden-key stop")
    armed = False
    value_touched = False

    class LazyMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            nonlocal value_touched
            value_touched = True
            raise AssertionError("route policy touched a poisoned forbidden value")

        def __iter__(self):  # type: ignore[no-untyped-def]
            nonlocal armed
            armed = True
            yield forbidden_key

        def __len__(self) -> int:
            return 1

    def check_cancelled() -> None:
        if armed:
            raise stop

    if forbidden_key == "api_key":
        call = lambda: embedding_compatibility_options(  # noqa: E731
            LazyMapping(),
            check_cancelled=check_cancelled,
        )
    else:
        call = lambda: resolve_inference_route(  # noqa: E731
            operation="chat",
            provider="openai",
            model="gpt-4.1",
            compatibility_options=LazyMapping(),
            environ={},
            check_cancelled=check_cancelled,
        )

    with pytest.raises(ValueError) as caught:
        call()
    assert caught.value is not stop
    assert not value_touched


def test_openai_route_stops_before_unread_unsupported_value() -> None:
    stop = StopIteration("injected OpenAI option-key stop")
    armed = False
    value_touched = False

    class LazyOptions(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            nonlocal value_touched
            value_touched = True
            raise AssertionError("OpenAI route touched an unsupported option value")

        def __iter__(self):  # type: ignore[no-untyped-def]
            nonlocal armed
            armed = True
            yield "temperature"

        def __len__(self) -> int:
            return 1

    def check_cancelled() -> None:
        if armed:
            raise stop

    with pytest.raises(StopIteration) as caught:
        resolve_inference_route(
            operation="embeddings",
            provider="openai",
            model="text-embedding-3-small",
            dimension=4,
            compatibility_options=LazyOptions(),
            environ={},
            check_cancelled=check_cancelled,
        )
    assert caught.value is stop
    assert not value_touched


def test_openai_route_rejects_retained_current_value_before_future_stop() -> None:
    stop = StopIteration("injected retained OpenAI option stop")
    armed = False

    class LazyOptions(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            nonlocal armed
            assert key == "temperature"
            armed = True
            return 1

        def __iter__(self):  # type: ignore[no-untyped-def]
            yield "temperature"

        def __len__(self) -> int:
            return 1

    def check_cancelled() -> None:
        if armed:
            raise stop

    with pytest.raises(ValueError, match="only the dimensions") as caught:
        resolve_inference_route(
            operation="embeddings",
            provider="openai",
            model="text-embedding-3-small",
            dimension=4,
            compatibility_options=LazyOptions(),
            environ={},
            check_cancelled=check_cancelled,
        )
    assert caught.value is not stop


@pytest.mark.parametrize(
    "options",
    [
        {"temperature": {}},
        {"dimensions": {}},
        {"dimensions": []},
        {"encode_kwargs": {}},
        {"timeout": {"batch_size": 1}},
    ],
)
def test_openai_route_callback_preserves_pruned_option_semantics(
    options: dict[str, object],
) -> None:
    legacy = resolve_inference_route(
        operation="embeddings",
        provider="openai",
        model="text-embedding-3-small",
        dimension=4,
        compatibility_options=options,
        environ={},
    )
    callback = resolve_inference_route(
        operation="embeddings",
        provider="openai",
        model="text-embedding-3-small",
        dimension=4,
        compatibility_options=options,
        environ={},
        check_cancelled=lambda: None,
    )
    assert callback == legacy


@pytest.mark.parametrize("artifact", [False, True])
def test_route_policy_attests_current_lazy_value_before_future_stop(
    artifact: bool,
) -> None:
    stop = StopIteration("injected route current-value stop")
    armed = False
    value_touched = False
    key = "embedding_route" if artifact else "dimensions"

    class LazyOptions(Mapping[str, object]):
        def __getitem__(self, current_key: str) -> object:
            nonlocal armed, value_touched
            assert current_key == key
            value_touched = True
            armed = True
            return 42 if artifact else 5

        def __iter__(self):  # type: ignore[no-untyped-def]
            yield key

        def __len__(self) -> int:
            return 1

    def check_cancelled() -> None:
        if armed:
            raise stop

    with pytest.raises(ValueError) as caught:
        if artifact:
            resolve_embedding_artifact_route(
                LazyOptions(),  # type: ignore[arg-type]
                environ={},
                check_cancelled=check_cancelled,
            )
        else:
            resolve_inference_route(
                operation="embeddings",
                provider="openai",
                model="text-embedding-3-small",
                dimension=4,
                compatibility_options=LazyOptions(),
                environ={},
                check_cancelled=check_cancelled,
            )
    assert caught.value is not stop
    assert value_touched


def test_route_mapping_preserves_scalar_key_and_integer_domain() -> None:
    assert list(
        provider_routes_module._json_mapping(
            {10: "ten", 2: "two"},  # type: ignore[dict-item]
            source="test route",
            check_cancelled=lambda: None,
        )
    ) == ["2", "10"]
    with pytest.raises(ValueError, match="JSON-compatible"):
        provider_routes_module._json_mapping(
            {2: "two", "a": "text"},  # type: ignore[dict-item]
            source="test route",
            check_cancelled=lambda: None,
        )
    with pytest.raises(ValueError, match="JSON-compatible"):
        embedding_compatibility_options(
            {"value": 10**5000},
            check_cancelled=lambda: None,
        )


@pytest.mark.parametrize("kind", ["list", "dict"])
def test_route_mapping_rejects_cycles_like_legacy(kind: str) -> None:
    cyclic: object
    if kind == "list":
        items: list[object] = []
        items.append(items)
        cyclic = items
    else:
        mapping: dict[str, object] = {}
        mapping["self"] = mapping
        cyclic = mapping
    with pytest.raises(ValueError, match="JSON-compatible"):
        provider_routes_module._json_mapping(
            {"value": cyclic},
            source="test route",
            check_cancelled=lambda: None,
        )


def test_route_long_key_canonicalization_and_sort_are_interruptible() -> None:
    stop = StopIteration("injected long-key stop")
    polls = 0

    def check_cancelled() -> None:
        nonlocal polls
        polls += 1
        if polls == 2:
            raise stop

    with pytest.raises(StopIteration) as caught:
        embedding_compatibility_options(
            {"x" * (16 * 1024 * 1024): 1},
            check_cancelled=check_cancelled,
        )
    assert caught.value is stop

    common = "x" * (128 * 1024)
    polls = 0
    with pytest.raises(StopIteration) as sort_caught:
        provider_routes_module._sorted_route_keys(
            {common + "b": 1, common + "a": 2},
            check_cancelled=check_cancelled,
        )
    assert sort_caught.value is stop


@pytest.mark.parametrize(
    "function,args",
    [
        (normalize_provider, ("openai",)),
        (normalize_endpoint, ("https://example.test",)),
    ],
)
def test_route_public_fast_paths_reject_noncallable_callback(
    function: object,
    args: tuple[object, ...],
) -> None:
    with pytest.raises(TypeError, match="callable"):
        function(*args, check_cancelled=17)  # type: ignore[operator]


@pytest.mark.parametrize(
    ("left", "right"),
    ((-0.0, 0.0), (True, 1), (1, 1.0)),
)
def test_frozen_route_options_preserve_canonical_scalar_identity(
    left: object,
    right: object,
) -> None:
    def route(value: object):  # type: ignore[no-untyped-def]
        return resolve_inference_route(
            operation="chat",
            provider="openai",
            model="test-model",
            compatibility_options={"value": value},
            environ={},
            check_cancelled=lambda: None,
        )

    left_route = route(left)
    right_route = route(right)

    assert left_route != right_route
    assert hash(left_route) != hash(right_route)
    assert left_route.compatibility_fingerprint != (
        right_route.compatibility_fingerprint
    )


def test_route_none_callback_keeps_legacy_json_helper_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_mapping = provider_routes_module._json_mapping
    calls = 0

    def legacy_mapping(
        value: Mapping[str, object] | None,
        *,
        source: str,
    ) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return real_mapping(value, source=source)

    monkeypatch.setattr(provider_routes_module, "_json_mapping", legacy_mapping)
    assert embedding_compatibility_options({"dimension": 4}) == {"dimension": 4}
    assert calls == 1


def test_embedding_option_pruning_polls_its_own_large_phase() -> None:
    option_count = 100_000
    options = {
        "".join("_" if encoded & (1 << bit) else "-" for bit in range(17))
        + "timeout": 1
        for encoded in range(option_count)
    }
    stop = RuntimeError("injected option-pruning stop")
    polls = 0

    def check_cancelled() -> None:
        nonlocal polls
        polls += 1
        if polls == option_count * 2:
            raise stop

    with pytest.raises(RuntimeError) as caught:
        embedding_compatibility_options(
            options,
            check_cancelled=check_cancelled,
        )
    assert caught.value is stop


def test_route_fingerprint_polls_before_hash_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = resolve_inference_route(
        operation="chat",
        provider="openai",
        model="test-model",
        compatibility_options={"value": "x" * (128 * 1024)},
        environ={},
    )
    stop = StopIteration("injected fingerprint hashing stop")
    armed = False
    updates = 0
    real_sha256 = provider_routes_module.hashlib.sha256

    class TrackingHash:
        def __init__(self) -> None:
            nonlocal armed
            armed = True
            self._digest = real_sha256()

        def update(self, value: bytes) -> None:
            nonlocal updates
            updates += 1
            self._digest.update(value)

        def hexdigest(self) -> str:
            return self._digest.hexdigest()

    def check_cancelled() -> None:
        if armed:
            raise stop

    monkeypatch.setattr(provider_routes_module.hashlib, "sha256", TrackingHash)
    with pytest.raises(StopIteration) as caught:
        route.interruptible_compatibility_fingerprint(check_cancelled)
    assert caught.value is stop
    assert updates == 0


def test_route_json_scalar_equality_attests_current_chunk() -> None:
    stop = RuntimeError("injected route equality stop")
    polls = 0

    def check_cancelled() -> None:
        nonlocal polls
        polls += 1
        raise stop

    assert not provider_routes_module._route_json_equal_interruptibly(
        "a" + "x" * (128 * 1024),
        "b" + "x" * (128 * 1024),
        check_cancelled=check_cancelled,
    )
    assert polls == 0

    left = "".join(("x" * (64 * 1024), "x" * (64 * 1024)))
    right = "".join(("x" * (64 * 1024), "x" * (64 * 1024)))
    assert left is not right
    with pytest.raises(RuntimeError) as caught:
        provider_routes_module._route_json_equal_interruptibly(
            left,
            right,
            check_cancelled=check_cancelled,
        )
    assert caught.value is stop


def test_openai_route_uses_explicit_token_without_storing_it() -> None:
    route = resolve_inference_route(
        operation="embeddings",
        provider="openai",
        model="text-embedding-3-small",
        endpoint="https://inference.example.test/v1",
        dimension=1536,
        credential_env="MODELS_TOKEN",
        environ={"MODELS_TOKEN": "action-secret"},
    )

    assert route.provider == "openai"
    assert route.endpoint == "https://inference.example.test/v1"
    assert route.credential_env == "MODELS_TOKEN"
    assert route.client_kwargs({"MODELS_TOKEN": "action-secret"}) == {
        "base_url": "https://inference.example.test/v1",
        "api_key": "action-secret",
    }
    serialized = json.dumps(route.public_identity(), sort_keys=True)
    assert "action-secret" not in serialized
    assert "MODELS_TOKEN" not in serialized


def test_openai_route_uses_explicit_credential_then_default() -> None:
    explicit = resolve_inference_route(
        operation="chat",
        provider="openai",
        model="gpt-4.1",
        credential_env="MODELS_TOKEN",
        environ={"OPENAI_API_KEY": "default", "MODELS_TOKEN": "explicit"},
    )
    fallback = resolve_inference_route(
        operation="chat",
        provider="openai",
        model="gpt-4.1",
        environ={"OPENAI_API_KEY": "default"},
    )

    assert explicit.client_model == "openai/gpt-4.1"
    assert explicit.credential_env == "MODELS_TOKEN"
    assert explicit.credential({"MODELS_TOKEN": "explicit"}) == "explicit"
    assert fallback.credential_env == "OPENAI_API_KEY"


def test_openai_route_reports_missing_credentials_without_token_data() -> None:
    route = resolve_inference_route(
        operation="embeddings",
        provider="openai",
        model="text-embedding-3-small",
        dimension=1536,
        environ={},
    )

    with pytest.raises(ValueError, match="OPENAI_API_KEY") as error:
        route.client_kwargs({})
    assert "Bearer" not in str(error.value)


@pytest.mark.parametrize("provider", ["github-models", "github_models"])
def test_retired_github_models_route_is_rejected(provider: str) -> None:
    with pytest.raises(ValueError, match="retired on 2026-07-30"):
        resolve_inference_route(
            operation="chat",
            provider=provider,
            model="openai/gpt-4.1",
        )


@pytest.mark.parametrize(
    "value",
    [
        "inference.example.test/v1",
        "https://token@inference.example.test/v1",
        "https://inference.example.test/v1?api_key=secret",
        "https://inference.example.test/v1#fragment",
        "https://inference.example.test/v1/../other",
        "https://inference.example.test/v1/embeddings",
        "https://inference.example.test/v1/%65mbeddings",
    ],
)
def test_endpoint_normalization_rejects_unsafe_or_operation_urls(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_endpoint(value)


def test_endpoint_normalization_is_stable() -> None:
    assert (
        normalize_endpoint("HTTPS://INFERENCE.EXAMPLE.TEST/v1/")
        == "https://inference.example.test/v1"
    )


def test_embedding_fingerprint_tracks_vector_semantics_not_runtime_knobs() -> None:
    base = dict(
        operation="embeddings",
        provider="huggingface",
        model="model/revision",
        dimension=768,
    )
    route = resolve_inference_route(
        **base,
        compatibility_options={
            "revision": "abc",
            "max_seq_length": 2048,
            "encode_kwargs": {
                "batch_size": 4,
                "normalize_embeddings": True,
            },
        },
    )
    other_batch = resolve_inference_route(
        **base,
        compatibility_options={
            "revision": "abc",
            "max_seq_length": 2048,
            "encode_kwargs": {
                "batch_size": 64,
                "normalize_embeddings": True,
            },
        },
    )
    different_prompt = resolve_inference_route(
        **base,
        compatibility_options={
            "revision": "abc",
            "max_seq_length": 2048,
            "query_prompt": "Represent this query: ",
            "encode_kwargs": {"normalize_embeddings": True},
        },
    )

    assert route.compatibility_fingerprint == other_batch.compatibility_fingerprint
    assert route.compatibility_fingerprint != different_prompt.compatibility_fingerprint
    assert route.compatibility_options["encode_kwargs"] == {
        "normalize_embeddings": True
    }


def test_embedding_fingerprint_tracks_execution_device() -> None:
    cpu = resolve_inference_route(
        operation="embeddings",
        provider="huggingface",
        model="vendor/model",
        dimension=768,
        compatibility_options={"encode_kwargs": {"device": "cpu"}},
    )
    cuda = resolve_inference_route(
        operation="embeddings",
        provider="huggingface",
        model="vendor/model",
        dimension=768,
        compatibility_options={"encode_kwargs": {"device": "cuda"}},
    )

    assert cpu.compatibility_fingerprint != cuda.compatibility_fingerprint


@pytest.mark.parametrize(
    "options",
    [
        {"api_key": "secret"},
        {"headers": {"Authorization": "Bearer secret"}},
        {"model_kwargs": {"access_token": "secret"}},
        {"fallbacks": [{"api_key": "secret"}]},
        {"base_url": "https://example.test/v1"},
    ],
)
def test_embedding_artifact_options_reject_credentials_and_endpoints(options) -> None:
    with pytest.raises(ValueError):
        embedding_compatibility_options(options)


def test_chat_compatibility_options_reject_credentials() -> None:
    with pytest.raises(ValueError, match="must not contain credentials"):
        resolve_inference_route(
            operation="chat",
            provider="openai",
            model="gpt-4.1",
            compatibility_options={"headers": {"Authorization": "Bearer secret"}},
        )

    with pytest.raises(ValueError, match="dedicated endpoint"):
        resolve_inference_route(
            operation="chat",
            provider="openai",
            model="gpt-4.1",
            compatibility_options={"api_base": "https://example.test/v1"},
        )


def test_openai_custom_endpoint_can_be_intentionally_unauthenticated() -> None:
    route = resolve_inference_route(
        operation="embeddings",
        provider="openai",
        model="served-embedding",
        endpoint="http://localhost:8080/v1/",
        dimension=1024,
        environ={},
    )

    assert route.client_kwargs({}) == {"base_url": "http://localhost:8080/v1"}


def test_openai_embedding_dimensions_are_request_options() -> None:
    route = resolve_inference_route(
        operation="embeddings",
        provider="openai",
        model="text-embedding-3-small",
        dimension=512,
        compatibility_options={"dimensions": 512},
    )

    assert route.embedding_backend_kwargs() == {"request_options": {"dimensions": 512}}
    with pytest.raises(ValueError, match="must equal"):
        resolve_inference_route(
            operation="embeddings",
            provider="openai",
            model="text-embedding-3-small",
            dimension=512,
            compatibility_options={"dimensions": 256},
        )


def test_huggingface_route_rejects_remote_configuration() -> None:
    with pytest.raises(ValueError, match="local Hugging Face"):
        resolve_inference_route(
            operation="embeddings",
            provider="huggingface",
            model="nomic-ai/CodeRankEmbed",
            endpoint="https://example.test/v1",
            dimension=768,
        )


def test_runtime_options_are_provider_specific_and_cannot_override_semantics() -> None:
    assert validate_embedding_runtime_options(
        {"api-key": "secret", "max-retries": 2},
        provider="openai",
    ) == {"api_key": "secret", "max_retries": 2}
    assert validate_embedding_runtime_options(
        {"encode_kwargs": {"batch-size": 8}},
        provider="huggingface",
    ) == {"encode_kwargs": {"batch_size": 8}}

    with pytest.raises(ValueError, match="vector compatibility"):
        validate_embedding_runtime_options(
            {"query_prompt": "changed"},
            provider="huggingface",
        )
    with pytest.raises(ValueError, match="vector semantics"):
        validate_embedding_runtime_options(
            {"encode_kwargs": {"normalize_embeddings": False}},
            provider="huggingface",
        )


def test_schema_v3_artifact_route_round_trips_and_rejects_drift() -> None:
    route = resolve_inference_route(
        operation="embeddings",
        provider="openai",
        model="text-embedding-3-small",
        endpoint="https://inference.example.test/v1",
        dimension=1536,
        environ={},
    )
    artifact = {
        "builder_schema": 3,
        "embedding_model": route.model,
        "embedding_provider": route.provider,
        "embedding_dimension": route.dimension,
        "dimension": route.dimension,
        "embedding_endpoint": route.endpoint,
        "embedding_kwargs": route.compatibility_options,
        "embedding_route": route.public_identity(),
        "embedding_fingerprint": route.compatibility_fingerprint,
    }

    reopened = resolve_embedding_artifact_route(artifact, environ={})
    assert reopened.public_identity() == route.public_identity()

    drifted = json.loads(json.dumps(artifact))
    drifted["embedding_route"]["model"] = "openai/text-embedding-3-large"
    with pytest.raises(ValueError, match="disagrees|fingerprint"):
        resolve_embedding_artifact_route(drifted, environ={})


def test_legacy_artifact_route_is_supported_but_private_kwargs_are_not() -> None:
    route = resolve_embedding_artifact_route(
        {
            "builder_schema": 2,
            "embedding_model": "vendor/model",
            "embedding_provider": "huggingface",
            "embedding_dimension": 384,
            "embedding_kwargs": {
                "revision": "immutable",
                "encode_kwargs": {"batch_size": 16},
            },
        }
    )
    assert route.model == "vendor/model"
    assert route.compatibility_options == {"revision": "immutable"}

    with pytest.raises(ValueError, match="credentials"):
        resolve_embedding_artifact_route(
            {
                "embedding_model": "served-model",
                "embedding_provider": "openai",
                "embedding_dimension": 384,
                "embedding_kwargs": {"api_key": "persisted-secret"},
            }
        )
