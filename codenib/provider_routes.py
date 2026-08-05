# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Secret-free provider identities and process-local inference routes."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping
from urllib.parse import unquote, urlsplit, urlunsplit

InferenceOperation = Literal["chat", "embeddings"]

INFERENCE_ROUTE_SCHEMA = "codenib.inference-route.v1"

_PROVIDER_ALIASES = {
    "hugging-face": "huggingface",
    "hugging_face": "huggingface",
}
_RETIRED_PROVIDERS = {"github-models", "github_models"}
_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_OPERATION_PATH_SUFFIXES = (
    "/chat/completions",
    "/completions",
    "/embeddings",
    "/responses",
)
_SENSITIVE_OPTION_KEYS = {
    "apikey",
    "authorization",
    "authtoken",
    "bearertoken",
    "clientsecret",
    "credential",
    "credentials",
    "headers",
    "password",
    "privatekey",
    "secret",
    "secretkey",
    "token",
}
_ENDPOINT_OPTION_KEYS = {"apibase", "baseurl", "endpoint"}
_RUNTIME_TOP_LEVEL_KEYS = {
    "batchsize",
    "cachefolder",
    "defaultbatchsize",
    "maxretries",
    "retry",
    "retries",
    "showprogressbar",
    "timeout",
}
_RUNTIME_ENCODE_KEYS = {
    "batchsize": "batch_size",
    "converttotensor": "convert_to_tensor",
    "converttonumpy": "convert_to_numpy",
    "showprogressbar": "show_progress_bar",
}
_RUNTIME_MODEL_KEYS = {
    "cachedir": "cache_dir",
    "cachefolder": "cache_folder",
    "localfilesonly": "local_files_only",
}
_REMOTE_RUNTIME_KEYS = {
    "apikey": "api_key",
    "baseurl": "base_url",
    "maxretries": "max_retries",
    "timeout": "timeout",
}
_HUGGINGFACE_RUNTIME_KEYS = {
    "cachefolder": "cache_folder",
    "defaultbatchsize": "default_batch_size",
}


def normalize_provider(value: str) -> str:
    """Return the canonical provider identifier used in persisted metadata."""

    provider = str(value or "").strip().lower()
    if provider in _RETIRED_PROVIDERS:
        raise ValueError(
            "GitHub Models was retired on 2026-07-30; select a current "
            "provider or an OpenAI-compatible BYO endpoint"
        )
    provider = _PROVIDER_ALIASES.get(provider, provider)
    if not _PROVIDER_RE.fullmatch(provider):
        raise ValueError(f"invalid inference provider: {value!r}")
    return provider


def normalize_endpoint(value: str | None) -> str | None:
    """Normalize a provider API base while rejecting credential-bearing URLs."""

    if value is None or not str(value).strip():
        return None
    raw = str(value).strip()
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("provider endpoint must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("provider endpoint must not contain user information")
    if parsed.query or parsed.fragment:
        raise ValueError("provider endpoint must not contain a query or fragment")

    decoded_path = unquote(parsed.path)
    if any(part in {".", ".."} for part in decoded_path.split("/")):
        raise ValueError("provider endpoint must not contain path traversal")
    if any(ord(character) < 32 for character in decoded_path):
        raise ValueError("provider endpoint contains control characters")
    path = parsed.path.rstrip("/")
    if path.lower().endswith(_OPERATION_PATH_SUFFIXES) or decoded_path.lower().rstrip(
        "/"
    ).endswith(_OPERATION_PATH_SUFFIXES):
        raise ValueError("provider endpoint must not include an operation path")

    host = parsed.hostname.lower()
    host = f"[{host}]" if ":" in host else host
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("provider endpoint has an invalid port") from exc
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))


def _canonical_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _json_mapping(value: Mapping[str, Any] | None, *, source: str) -> dict[str, Any]:
    if value is not None and not isinstance(value, Mapping):
        raise ValueError(f"{source} must be a mapping")
    try:
        encoded = json.dumps(
            dict(value or {}),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source} must contain JSON-compatible values") from exc
    return dict(json.loads(encoded))


def _reject_private_options(
    value: Any,
    *,
    source: str,
    path: tuple[str, ...] = (),
) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            canonical = _canonical_key(key)
            if canonical in _SENSITIVE_OPTION_KEYS or canonical.endswith(
                ("apikey", "accesstoken", "clientsecret")
            ):
                location = ".".join((*path, key))
                raise ValueError(f"{source} must not contain credentials: {location}")
            _reject_private_options(
                item,
                source=source,
                path=(*path, key),
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_private_options(
                item,
                source=source,
                path=(*path, str(index)),
            )


def _reject_endpoint_options(
    value: Any,
    *,
    source: str,
    path: tuple[str, ...] = (),
) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _canonical_key(key) in _ENDPOINT_OPTION_KEYS:
                location = ".".join((*path, key))
                raise ValueError(
                    f"{source} must use the dedicated endpoint field: {location}"
                )
            _reject_endpoint_options(item, source=source, path=(*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_endpoint_options(
                item,
                source=source,
                path=(*path, str(index)),
            )


def embedding_compatibility_options(
    value: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return only public options that can affect produced embedding vectors."""

    options = _json_mapping(value, source="embedding options")
    _reject_private_options(options, source="embedding artifact options")

    def visit(value: Any, path: tuple[str, ...]) -> Any:
        if isinstance(value, list):
            return [
                visit(item, (*path, str(index))) for index, item in enumerate(value)
            ]
        if not isinstance(value, Mapping):
            return value

        result: dict[str, Any] = {}
        for key, item in value.items():
            canonical = _canonical_key(key)
            if canonical in _ENDPOINT_OPTION_KEYS:
                raise ValueError(
                    f"embedding endpoint must use the dedicated endpoint field: {key}"
                )

            operational = not path and canonical in _RUNTIME_TOP_LEVEL_KEYS
            if path and _canonical_key(path[-1]) == "encodekwargs":
                operational = operational or canonical in _RUNTIME_ENCODE_KEYS
            if path and _canonical_key(path[-1]) == "modelkwargs":
                operational = operational or canonical in _RUNTIME_MODEL_KEYS
            if operational:
                continue

            nested = visit(item, (*path, key))
            if nested not in ({}, []):
                result[key] = nested
        return result

    return visit(options, ())


def validate_embedding_runtime_options(
    value: Mapping[str, Any] | None,
    *,
    provider: str,
) -> dict[str, Any]:
    """Accept only process-local knobs that cannot change vector semantics."""

    if value is not None and not isinstance(value, Mapping):
        raise ValueError("embedding runtime options must be a mapping")
    canonical_provider = normalize_provider(provider)
    local = canonical_provider == "huggingface"
    allowed = _HUGGINGFACE_RUNTIME_KEYS if local else _REMOTE_RUNTIME_KEYS
    options = dict(value or {})
    validated: dict[str, Any] = {}
    for key, item in options.items():
        canonical = _canonical_key(key)
        if canonical in allowed:
            validated[allowed[canonical]] = item
            continue
        if local and canonical == "encodekwargs" and isinstance(item, Mapping):
            invalid = [
                nested
                for nested in item
                if _canonical_key(nested) not in _RUNTIME_ENCODE_KEYS
            ]
            if invalid:
                raise ValueError(
                    "embedding runtime encode_kwargs may not override vector "
                    f"semantics: {', '.join(sorted(invalid))}"
                )
            validated["encode_kwargs"] = {
                _RUNTIME_ENCODE_KEYS[_canonical_key(nested)]: nested_value
                for nested, nested_value in item.items()
            }
            continue
        if local and canonical == "modelkwargs" and isinstance(item, Mapping):
            invalid = [
                nested
                for nested in item
                if _canonical_key(nested) not in _RUNTIME_MODEL_KEYS
            ]
            if invalid:
                raise ValueError(
                    "embedding runtime model_kwargs may not override vector "
                    f"semantics: {', '.join(sorted(invalid))}"
                )
            validated["model_kwargs"] = {
                _RUNTIME_MODEL_KEYS[_canonical_key(nested)]: nested_value
                for nested, nested_value in item.items()
            }
            continue
        raise ValueError(
            "embedding runtime option may affect vector compatibility; "
            f"declare it in embedding_kwargs instead: {key}"
        )
    return validated


def _normalize_model(model: str) -> str:
    normalized = str(model or "").strip()
    if not normalized or any(character.isspace() for character in normalized):
        raise ValueError("inference model must be a non-empty id without whitespace")
    if any(ord(character) < 32 for character in normalized):
        raise ValueError("inference model contains control characters")
    return normalized


def _normalize_credential_env(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    name = str(value).strip()
    if not _ENV_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid credential environment variable: {value!r}")
    return name


@dataclass(frozen=True, slots=True)
class InferenceRoute:
    """A public compatibility identity plus a process-local credential source."""

    operation: InferenceOperation
    provider: str
    model: str
    client_model: str
    endpoint: str | None = None
    dimension: int | None = None
    credential_env: str | None = None
    credential_required: bool = False
    _options_json: str = field(default="{}", repr=False)

    @property
    def compatibility_options(self) -> dict[str, Any]:
        return dict(json.loads(self._options_json))

    def public_identity(self) -> dict[str, Any]:
        """Return the complete secret-free identity used for compatibility."""

        identity: dict[str, Any] = {
            "schema": INFERENCE_ROUTE_SCHEMA,
            "operation": self.operation,
            "provider": self.provider,
            "model": self.model,
            "endpoint": self.endpoint,
            "options": self.compatibility_options,
        }
        if self.dimension is not None:
            identity["dimension"] = self.dimension
        return identity

    def embedding_backend_kwargs(self) -> dict[str, Any]:
        """Map compatibility options to the selected embedding wrapper."""

        if self.operation != "embeddings":
            raise ValueError("chat routes do not have embedding backend kwargs")
        options = self.compatibility_options
        if self.provider == "huggingface":
            return options
        return {"request_options": options} if options else {}

    @property
    def compatibility_fingerprint(self) -> str:
        payload = json.dumps(
            self.public_identity(),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def credential(self, environ: Mapping[str, str] | None = None) -> str | None:
        """Resolve the selected credential without storing it on the route."""

        environment = os.environ if environ is None else environ
        value = environment.get(self.credential_env, "") if self.credential_env else ""
        if value:
            return value
        if self.credential_required:
            name = self.credential_env or "an explicit credential variable"
            raise ValueError(
                f"credential environment variable is unset or empty: {name}"
            )
        return None

    def client_kwargs(
        self,
        environ: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Resolve process-local SDK kwargs; callers must never persist this value."""

        result: dict[str, str] = {}
        if self.endpoint:
            key = "api_base" if self.operation == "chat" else "base_url"
            result[key] = self.endpoint
        credential = self.credential(environ)
        if credential:
            result["api_key"] = credential
        return result


def resolve_inference_route(
    *,
    operation: InferenceOperation,
    provider: str,
    model: str,
    endpoint: str | None = None,
    dimension: int | None = None,
    credential_env: str | None = None,
    compatibility_options: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> InferenceRoute:
    """Resolve an explicit provider route without importing an inference SDK."""

    if operation not in {"chat", "embeddings"}:
        raise ValueError(f"unsupported inference operation: {operation!r}")
    canonical_provider = normalize_provider(provider)
    canonical_model = _normalize_model(model)
    normalized_endpoint = normalize_endpoint(endpoint)
    environment = os.environ if environ is None else environ
    selected_env = _normalize_credential_env(credential_env)
    credential_was_explicit = selected_env is not None

    if canonical_provider == "huggingface":
        if operation != "embeddings":
            raise ValueError("huggingface is supported only for embeddings")
        if normalized_endpoint or selected_env:
            raise ValueError(
                "local Hugging Face embeddings do not accept an endpoint or API key"
            )
        client_model = canonical_model
        credential_required = False
    elif canonical_provider == "openai":
        if selected_env is None and environment.get("OPENAI_API_KEY"):
            selected_env = "OPENAI_API_KEY"
        if selected_env is None and normalized_endpoint is None:
            selected_env = "OPENAI_API_KEY"
        client_model = (
            canonical_model
            if operation == "embeddings" or canonical_model.startswith("openai/")
            else f"openai/{canonical_model}"
        )
        credential_required = normalized_endpoint is None or credential_was_explicit
    else:
        if operation == "embeddings":
            raise ValueError(
                f"unsupported embedding provider: {canonical_provider}; use "
                "huggingface or openai"
            )
        client_model = (
            canonical_model
            if canonical_model.startswith(f"{canonical_provider}/")
            else f"{canonical_provider}/{canonical_model}"
        )
        credential_required = credential_was_explicit

    if operation == "embeddings":
        if (
            not isinstance(dimension, int)
            or isinstance(dimension, bool)
            or dimension <= 0
        ):
            raise ValueError("embedding dimension must be a positive integer")
        options = embedding_compatibility_options(compatibility_options)
        if canonical_provider == "openai":
            unsupported = sorted(set(options) - {"dimensions"})
            if unsupported:
                raise ValueError(
                    "OpenAI-compatible embedding routes support only the dimensions "
                    f"compatibility option, not: {', '.join(unsupported)}"
                )
            requested_dimension = options.get("dimensions")
            if requested_dimension is not None and requested_dimension != dimension:
                raise ValueError(
                    "embedding dimensions option must equal the artifact dimension"
                )
    else:
        if dimension is not None:
            raise ValueError("chat routes do not accept an embedding dimension")
        options = _json_mapping(
            compatibility_options,
            source="chat compatibility options",
        )
        _reject_private_options(options, source="chat compatibility options")
        _reject_endpoint_options(options, source="chat compatibility options")
    options_json = json.dumps(
        options,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return InferenceRoute(
        operation=operation,
        provider=canonical_provider,
        model=canonical_model,
        client_model=client_model,
        endpoint=normalized_endpoint,
        dimension=dimension,
        credential_env=selected_env,
        credential_required=credential_required,
        _options_json=options_json,
    )


def resolve_embedding_artifact_route(
    config: Mapping[str, Any],
    *,
    credential_env: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> InferenceRoute:
    """Resolve and validate the immutable embedding route in an artifact.

    Schema-v3 artifacts carry a canonical route and fingerprint. Older
    artifacts are reconstructed from their top-level embedding fields, while
    still rejecting credentials or endpoints hidden in ``embedding_kwargs``.
    """

    artifact = _json_mapping(config, source="vector artifact configuration")
    builder_schema = artifact.get("builder_schema")
    route_identity_required = (
        isinstance(builder_schema, int)
        and not isinstance(builder_schema, bool)
        and builder_schema >= 3
    )
    embedded = artifact.get("embedding_route")

    if route_identity_required and not isinstance(embedded, Mapping):
        raise ValueError("vector artifact is missing its embedding route identity")

    if embedded is not None:
        if not isinstance(embedded, Mapping):
            raise ValueError("vector artifact has an invalid embedding route identity")
        embedded_route = _json_mapping(
            embedded,
            source="vector artifact embedding route",
        )
        if embedded_route.get("schema") != INFERENCE_ROUTE_SCHEMA:
            raise ValueError(
                "vector artifact uses an unsupported embedding route schema"
            )
        if embedded_route.get("operation") != "embeddings":
            raise ValueError("vector artifact route is not an embedding route")
        route = resolve_inference_route(
            operation="embeddings",
            provider=embedded_route.get("provider", ""),
            model=embedded_route.get("model", ""),
            endpoint=embedded_route.get("endpoint"),
            dimension=embedded_route.get("dimension"),
            credential_env=credential_env,
            compatibility_options=embedded_route.get("options"),
            environ=environ,
        )
        if embedded_route != route.public_identity():
            raise ValueError("vector artifact embedding route is not canonical")
    else:
        route = resolve_inference_route(
            operation="embeddings",
            provider=artifact.get("embedding_provider", ""),
            model=artifact.get("embedding_model", ""),
            endpoint=artifact.get("embedding_endpoint"),
            dimension=artifact.get("dimension", artifact.get("embedding_dimension")),
            credential_env=credential_env,
            compatibility_options=artifact.get("embedding_kwargs"),
            environ=environ,
        )

    top_level_checks = {
        "embedding_model": route.model,
        "embedding_provider": route.provider,
        "embedding_dimension": route.dimension,
        "dimension": route.dimension,
        "embedding_endpoint": route.endpoint,
    }
    if route_identity_required:
        top_level_checks["embedding_kwargs"] = route.compatibility_options
    for key, expected in top_level_checks.items():
        if key not in artifact:
            if route_identity_required:
                raise ValueError(f"vector artifact is missing {key}")
            continue
        actual = artifact[key]
        if key == "embedding_provider" and isinstance(actual, str):
            actual = normalize_provider(actual)
        elif key == "embedding_endpoint":
            actual = normalize_endpoint(actual)
        elif key in {"embedding_dimension", "dimension"} and (
            not isinstance(actual, int) or isinstance(actual, bool)
        ):
            raise ValueError(f"vector artifact {key} is not a positive integer")
        if actual != expected:
            raise ValueError(f"vector artifact {key} disagrees with its route identity")

    fingerprint = artifact.get("embedding_fingerprint")
    if route_identity_required and not isinstance(fingerprint, str):
        raise ValueError("vector artifact is missing its embedding fingerprint")
    if fingerprint is not None and fingerprint != route.compatibility_fingerprint:
        raise ValueError("vector artifact embedding fingerprint mismatch")
    return route


__all__ = [
    "INFERENCE_ROUTE_SCHEMA",
    "InferenceRoute",
    "embedding_compatibility_options",
    "normalize_endpoint",
    "normalize_provider",
    "resolve_embedding_artifact_route",
    "resolve_inference_route",
    "validate_embedding_runtime_options",
]
